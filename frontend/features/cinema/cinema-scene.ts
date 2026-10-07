import { cameraView, perspective, photoLayout, photoPoint, type Vec3, type PhotoViewport } from "./scene-math";

export type CinemaBackground = "portal" | "world";
export type SceneMode = "webgl" | "fallback";
export interface CinemaSceneController {
  setPaused: (paused: boolean) => void;
  setBackground: (background: CinemaBackground) => void;
  dispose: () => void;
}
type SceneOptions = {
  canvas: HTMLCanvasElement;
  host: HTMLElement;
  images: Record<CinemaBackground, HTMLImageElement>;
  onMode: (mode: SceneMode) => void;
};
type Mesh = { buffer: WebGLBuffer; count: number; mode: number; texture: CinemaBackground | "background" | null };
type UV = readonly [number, number];
type Locations = {
  position: number; normal: number; uv: number;
  projection: WebGLUniformLocation | null; view: WebGLUniformLocation | null;
  tex: WebGLUniformLocation | null; mode: WebGLUniformLocation | null;
  eye: WebGLUniformLocation | null; time: WebGLUniformLocation | null; pixelRatio: WebGLUniformLocation | null;
};
const VERTEX = "attribute vec3 position;attribute vec3 normal;attribute vec2 uv;uniform mat4 projection;uniform mat4 view;varying vec2 texCoord;varying vec3 n;varying vec3 worldPos;void main(){texCoord=uv;n=normal;worldPos=position;gl_Position=projection*view*vec4(position,1.0);}";
const FRAGMENT = "precision mediump float;varying vec2 texCoord;varying vec3 n;varying vec3 worldPos;uniform sampler2D tex;uniform int mode;uniform vec3 eye;uniform float time;void main(){if(mode==0){vec2 p=texCoord;float water=1.0-smoothstep(.30,.49,p.y);p.x+=sin(p.y*135.0+time*.18)*.00038*water;p.y+=cos(p.x*125.0+time*.15)*.00025*water;vec3 c=texture2D(tex,p).rgb;gl_FragColor=vec4(c,1.0);}else if(mode==1){vec3 c=texture2D(tex,texCoord).rgb;gl_FragColor=vec4(c*1.02,1.0);}else if(mode==2){vec3 N=normalize(n);vec3 V=normalize(eye-worldPos);vec3 L=normalize(vec3(-2.0,4.0,5.0)-worldPos);float spec=pow(max(dot(reflect(-L,N),V),0.0),32.0);float fr=pow(1.0-max(dot(N,V),0.0),3.0);vec3 c=vec3(.035,.05,.062)+vec3(.07,.085,.10)*max(dot(N,L),0.0)+vec3(.35,.42,.44)*spec+vec3(.08,.13,.14)*fr;gl_FragColor=vec4(c,.34);}else{float pulse=.86+.08*sin(time*.65);gl_FragColor=vec4(vec3(.83,.95,.94)*pulse,1.0);}}";
const POINT_VERTEX = "attribute vec4 position;uniform mat4 projection;uniform mat4 view;uniform float time;uniform float pixelRatio;varying float light;void main(){vec3 p=position.xyz;p.x+=sin(time*.22+position.w)*.045;p.y+=sin(time*.17+position.w*2.0)*.13;vec4 eye=view*vec4(p,1.0);gl_Position=projection*eye;gl_PointSize=clamp((7.0+4.0*sin(position.w))*pixelRatio/(-eye.z)*3.0,1.0,8.0);light=.16+.20*(.5+.5*sin(position.w+time*.28));}";
const POINT_FRAGMENT = "precision mediump float;varying float light;void main(){float d=length(gl_PointCoord-vec2(.5))*2.0;if(d>1.0)discard;float a=pow(1.0-d,2.0)*light;gl_FragColor=vec4(vec3(.68,.88,1.0)*a,a);}";

function required<T>(value: T | null): T {
  if (value === null) throw new Error("The graphics device could not allocate a resource.");
  return value;
}

export function createCinemaScene({ canvas, host, images, onMode }: SceneOptions): CinemaSceneController | null {
  const context = canvas.getContext("webgl", {
    alpha: false, antialias: true, depth: true, preserveDrawingBuffer: true, powerPreference: "low-power",
  });
  if (!context) { onMode("fallback"); return null; }
  const gl: WebGLRenderingContext = context;
  const buffers = new Set<WebGLBuffer>(), programs = new Set<WebGLProgram>(), textures: Partial<Record<CinemaBackground, WebGLTexture>> = {};
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  let disposed = false, lost = false, paused = false, visible = true, background: CinemaBackground = "portal";
  let frameRequest = 0, lastFrame = 0, clock = 0, pixelRatio = 1, width = 0, height = 0, frames = 0;
  let mobileLayout = false;
  let camera: [number, number] = [0, 0], target: [number, number] = [0, 0];
  let meshes: Mesh[] = [], projection: Float32Array | undefined;
  let resizeObserver: ResizeObserver | undefined, intersectionObserver: IntersectionObserver | undefined;

  function newBuffer() {
    const buffer = required(gl.createBuffer()); buffers.add(buffer); return buffer;
  }
  function program(vertex: string, fragment: string) {
    const p = required(gl.createProgram()); programs.add(p);
    for (const [type, source] of [[gl.VERTEX_SHADER, vertex], [gl.FRAGMENT_SHADER, fragment]] as const) {
      const shader = required(gl.createShader(type));
      gl.shaderSource(shader, source); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        const message = gl.getShaderInfoLog(shader); gl.deleteShader(shader); throw new Error(message || "Shader compilation failed.");
      }
      gl.attachShader(p, shader); gl.deleteShader(shader);
    }
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) || "Shader linking failed.");
    return p;
  }
  function locations(p: WebGLProgram): Locations {
    return {
      position: gl.getAttribLocation(p, "position"), normal: gl.getAttribLocation(p, "normal"), uv: gl.getAttribLocation(p, "uv"),
      projection: gl.getUniformLocation(p, "projection"), view: gl.getUniformLocation(p, "view"),
      tex: gl.getUniformLocation(p, "tex"), mode: gl.getUniformLocation(p, "mode"), eye: gl.getUniformLocation(p, "eye"),
      time: gl.getUniformLocation(p, "time"), pixelRatio: gl.getUniformLocation(p, "pixelRatio"),
    };
  }
  function quad(out: number[], points: readonly Vec3[], normal: Vec3, uv: readonly UV[] = [[0, 1], [1, 1], [1, 0], [0, 0]]) {
    for (const i of [0, 1, 2, 0, 2, 3]) out.push(...points[i], ...normal, ...uv[i]);
  }
  function mesh(vertices: number[], mode: number, texture: Mesh["texture"]) {
    const buffer = newBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.STATIC_DRAW);
    meshes.push({ buffer, count: vertices.length / 8, mode, texture });
  }
  function releaseResources() {
    for (const buffer of buffers) gl.deleteBuffer(buffer); buffers.clear();
    for (const p of programs) gl.deleteProgram(p); programs.clear();
    for (const t of Object.values(textures)) gl.deleteTexture(t);
  }

  let main: WebGLProgram, particles: WebGLProgram, ml: Locations, pl: Locations, particleBuffer: WebGLBuffer;
  try {
    main = program(VERTEX, FRAGMENT); particles = program(POINT_VERTEX, POINT_FRAGMENT);
    ml = locations(main); pl = locations(particles); particleBuffer = newBuffer();
    for (const k of ["portal", "world"] as const) {
      const texture = required(gl.createTexture()); textures[k] = texture;
      gl.bindTexture(gl.TEXTURE_2D, texture); gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, images[k]);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    }
  } catch (error) { releaseResources(); throw error; }

  function resize() {
    if (disposed || lost) return;
    const rect = canvas.getBoundingClientRect(), w = Math.round(rect.width), h = Math.round(rect.height);
    const nextPixelRatio = Math.min(devicePixelRatio || 1, 1.5);
    // The mobile canvas starts below the copy; derive its crop from the rendered CSS layout.
    const nextMobileLayout = canvas.offsetTop > 0;
    if (!w || !h || (w === width && h === height && nextPixelRatio === pixelRatio && nextMobileLayout === mobileLayout)) return;
    width = w; height = h; pixelRatio = nextPixelRatio; mobileLayout = nextMobileLayout;
    canvas.width = Math.round(w * pixelRatio); canvas.height = Math.round(h * pixelRatio);
    gl.viewport(0, 0, canvas.width, canvas.height); projection = perspective(w / h);
    for (const m of meshes) { gl.deleteBuffer(m.buffer); buffers.delete(m.buffer); } meshes = [];
    const viewport: PhotoViewport = { width: w, height: h, mobile: mobileLayout };
    const layout = photoLayout(viewport), imageAspect = 1672 / 941;
    canvas.dataset.sceneFocus = String(layout.focus);
    canvas.dataset.sceneSize = `${w}x${h}`;
    const bgH = layout.halfHeight * Math.max(layout.aspect / imageAspect, 1) * 8 / 6;
    const bgW = bgH * imageAspect, shift = -(layout.focus - 0.5) * 2 * bgW;
    let vertices: number[] = [];
    quad(vertices, [[-bgW + shift, bgH, -2], [bgW + shift, bgH, -2], [bgW + shift, -bgH, -2], [-bgW + shift, -bgH, -2]], [0, 0, 1]);
    mesh(vertices, 0, "background");
    const outer: UV[] = [[0.679, 0.095], [0.820, 0.058], [0.820, 0.571], [0.679, 0.586]];
    const inner: UV[] = [[0.683, 0.105], [0.816, 0.069], [0.816, 0.568], [0.683, 0.583]];
    const coord = (point: UV, z: number) => photoPoint(point[0], point[1], z, viewport, 0.03);
    vertices = [];
    quad(vertices, inner.map(p => photoPoint(p[0], p[1], -0.10, viewport)), [0, 0, 1], inner.map(p => [p[0], 1 - p[1]] as UV));
    mesh(vertices, 1, "portal");
    const rim: number[] = [], light: number[] = [];
    // Leave the threshold open so the traveler can walk through the luminous frame.
    for (const i of [0, 1, 3]) {
      const j = (i + 1) % 4;
      const a = coord(outer[i], 0.03), b = coord(outer[j], 0.03), c = coord(inner[j], 0.03), d = coord(inner[i], 0.03);
      quad(rim, [a, b, c, d], [0, 0, 1]);
      const nx = d[1] - c[1], ny = c[0] - d[0], magnitude = Math.hypot(nx, ny) || 1;
      const edge: Vec3 = [nx / magnitude, ny / magnitude, 0];
      quad(rim, [d, c, coord(inner[j], -0.16), coord(inner[i], -0.16)], edge);
      quad(rim, [a, coord(outer[i], -0.16), coord(outer[j], -0.16), b], [-edge[0], -edge[1], 0]);
      const pa: UV = [outer[i][0] * 0.16 + inner[i][0] * 0.84, outer[i][1] * 0.16 + inner[i][1] * 0.84];
      const pb: UV = [outer[j][0] * 0.16 + inner[j][0] * 0.84, outer[j][1] * 0.16 + inner[j][1] * 0.84];
      quad(light, [coord(pa, 0.034), coord(pb, 0.034), coord(inner[j], 0.034), coord(inner[i], 0.034)], [0, 0, 1]);
    }
    mesh(rim, 2, null); mesh(light, 3, null);
    const pointData: number[] = []; let seed = 93;
    const random = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
    for (let i = 0; i < 92; i++) pointData.push((0.5 + random() * 4.9) * w / h / 2.15, -1.8 + random() * 5, -1.2 + random() * 3.7, random() * 30);
    gl.bindBuffer(gl.ARRAY_BUFFER, particleBuffer); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pointData), gl.STATIC_DRAW);
    render();
  }
  function render() {
    if (disposed || lost || !projection) return;
    gl.clearColor(0.03, 0.05, 0.08, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST); gl.depthMask(true); gl.disable(gl.BLEND);
    const eye: Vec3 = [camera[0], camera[1], 6], view = cameraView(eye);
    gl.useProgram(main); gl.uniformMatrix4fv(ml.projection, false, projection); gl.uniformMatrix4fv(ml.view, false, view);
    gl.uniform3fv(ml.eye, [...eye]); gl.uniform1f(ml.time, clock); gl.uniform1i(ml.tex, 0);
    for (const m of meshes) {
      gl.bindBuffer(gl.ARRAY_BUFFER, m.buffer);
      for (const [location, size, offset] of [[ml.position, 3, 0], [ml.normal, 3, 12], [ml.uv, 2, 24]]) {
        if (location >= 0) { gl.enableVertexAttribArray(location); gl.vertexAttribPointer(location, size, gl.FLOAT, false, 32, offset); }
      }
      gl.uniform1i(ml.mode, m.mode);
      if (m.mode === 2) { gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false); }
      else { gl.disable(gl.BLEND); gl.depthMask(true); }
      if (m.texture) { gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, textures[m.texture === "background" ? background : m.texture] ?? null); }
      gl.drawArrays(gl.TRIANGLES, 0, m.count);
    }
    for (const location of [ml.position, ml.normal, ml.uv]) if (location >= 0) gl.disableVertexAttribArray(location);
    gl.useProgram(particles); gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE); gl.depthMask(false);
    gl.uniformMatrix4fv(pl.projection, false, projection); gl.uniformMatrix4fv(pl.view, false, view);
    gl.uniform1f(pl.time, clock); gl.uniform1f(pl.pixelRatio, pixelRatio);
    gl.bindBuffer(gl.ARRAY_BUFFER, particleBuffer); gl.enableVertexAttribArray(pl.position);
    gl.vertexAttribPointer(pl.position, 4, gl.FLOAT, false, 16, 0); gl.drawArrays(gl.POINTS, 0, 92);
    gl.disableVertexAttribArray(pl.position); gl.depthMask(true); gl.disable(gl.BLEND);
    frames++; canvas.dataset.frames = String(frames); canvas.dataset.camera = JSON.stringify(eye);
    if (frames < 3 || frames % 60 === 0) canvas.dataset.glError = String(gl.getError());
  }
  function active() { return !disposed && !lost && !paused && !reduce.matches && visible && !document.hidden; }
  function tick(now: number) {
    frameRequest = 0; if (!active()) return;
    if (now - lastFrame > 32) {
      clock += Math.min((now - lastFrame) / 1000, 0.06); lastFrame = now;
      camera[0] += (target[0] - camera[0]) * 0.09; camera[1] += (target[1] - camera[1]) * 0.09; render();
    }
    frameRequest = requestAnimationFrame(tick);
  }
  function refresh() {
    if (disposed || lost) return;
    if (!active()) {
      cancelAnimationFrame(frameRequest); frameRequest = 0;
      if (paused || reduce.matches) { camera = [0, 0]; target = [0, 0]; host.style.setProperty("--px", "0"); host.style.setProperty("--py", "0"); }
      render();
    } else if (!frameRequest) { lastFrame = performance.now(); frameRequest = requestAnimationFrame(tick); }
  }
  function pointerMove(e: PointerEvent) {
    if (paused || reduce.matches || e.pointerType === "touch") return;
    const r = host.getBoundingClientRect(), x = (e.clientX - r.left) / r.width * 2 - 1, y = (e.clientY - r.top) / r.height * 2 - 1;
    target = [x * 0.38, -y * 0.20]; host.style.setProperty("--px", String(x)); host.style.setProperty("--py", String(y)); refresh();
  }
  function pointerLeave() { target = [0, 0]; host.style.setProperty("--px", "0"); host.style.setProperty("--py", "0"); }
  function contextLost(e: Event) { e.preventDefault(); lost = true; cancelAnimationFrame(frameRequest); frameRequest = 0; onMode("fallback"); }
  function dispose() {
    if (disposed) return; disposed = true; cancelAnimationFrame(frameRequest);
    resizeObserver?.disconnect(); intersectionObserver?.disconnect();
    host.removeEventListener("pointermove", pointerMove); host.removeEventListener("pointerleave", pointerLeave);
    canvas.removeEventListener("webglcontextlost", contextLost); document.removeEventListener("visibilitychange", refresh);
    window.removeEventListener("resize", resize);
    reduce.removeEventListener("change", refresh); releaseResources();
    host.style.removeProperty("--px"); host.style.removeProperty("--py");
  }
  try {
    host.addEventListener("pointermove", pointerMove); host.addEventListener("pointerleave", pointerLeave);
    canvas.addEventListener("webglcontextlost", contextLost); document.addEventListener("visibilitychange", refresh);
    window.addEventListener("resize", resize);
    reduce.addEventListener("change", refresh);
    resizeObserver = new ResizeObserver(resize); resizeObserver.observe(canvas);
    intersectionObserver = new IntersectionObserver(entries => { visible = entries[0].isIntersecting; refresh(); }, { threshold: 0.05 });
    intersectionObserver.observe(host);
    resize(); onMode("webgl"); refresh();
  } catch (error) { dispose(); throw error; }
  return { setPaused(value) { paused = value; refresh(); }, setBackground(value) { background = value; render(); }, dispose };
}
