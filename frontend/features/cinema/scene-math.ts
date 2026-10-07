export type Vec3 = readonly [number, number, number];
export type PhotoViewport = { width: number; height: number; mobile: boolean };

const PHOTO_WIDTH = 1672;
const PHOTO_HEIGHT = 941;
export const CAMERA_DISTANCE = 6;
export const FIELD_OF_VIEW = 43;

export function photoLayout({ width, height, mobile }: PhotoViewport) {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new RangeError("The scene viewport must have a positive finite size.");
  }
  const halfHeight = Math.tan(FIELD_OF_VIEW * Math.PI / 360) * CAMERA_DISTANCE;
  return {
    halfHeight,
    halfWidth: halfHeight * width / height,
    cover: Math.max(width / PHOTO_WIDTH, height / PHOTO_HEIGHT),
    focus: mobile ? 0.735 : 0.5,
    aspect: width / height,
  };
}

/** Place a photo pixel on a depth plane, keeping its initial screen alignment. */
export function photoPoint(
  u: number, v: number, depth: number, viewport: PhotoViewport, anchorDepth = depth,
): Vec3 {
  const layout = photoLayout(viewport);
  const scale = (CAMERA_DISTANCE - anchorDepth) / CAMERA_DISTANCE;
  return [
    (u - layout.focus) * PHOTO_WIDTH * layout.cover / viewport.width * 2 * layout.halfWidth * scale,
    (0.5 - v) * PHOTO_HEIGHT * layout.cover / viewport.height * 2 * layout.halfHeight * scale,
    depth,
  ];
}

function normalize(v: Vec3): Vec3 {
  const magnitude = Math.hypot(...v) || 1;
  return [v[0] / magnitude, v[1] / magnitude, v[2] / magnitude];
}
function cross(a: Vec3, b: Vec3): Vec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
function dot(a: Vec3, b: Vec3) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}
export function cameraView(eye: Vec3) {
  const z = normalize(eye), x = normalize(cross([0, 1, 0], z)), y = cross(z, x);
  return new Float32Array([
    x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0,
    -dot(x, eye), -dot(y, eye), -dot(z, eye), 1,
  ]);
}
export function perspective(aspect: number) {
  if (!Number.isFinite(aspect) || aspect <= 0) throw new RangeError("Invalid aspect ratio.");
  const f = 1 / Math.tan(FIELD_OF_VIEW * Math.PI / 360), near = 0.1, far = 80;
  return new Float32Array([
    f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / (near - far), -1,
    0, 0, 2 * far * near / (near - far), 0,
  ]);
}

