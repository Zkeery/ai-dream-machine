import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";
import * as workflow from "../lib/workflow.ts";

const require = createRequire(import.meta.url), ts = require("typescript");
const catalog = { provider: "AIHubMix", groups: Object.fromEntries([
  ["text", "文本", ["qwen3.5-plus", "qwen3.5-flash"]], ["image", "图片", ["qwen-image-2.0", "qwen-image-2.0-pro"]],
  ["video_first_frame", "首帧视频", ["wan2.7-i2v", "wan2.6-i2v"]], ["video_start_end", "首尾帧视频", ["wan2.7-i2v"]], ["video_reference", "参考图视频", ["wan2.7-r2v"]],
  ["video_speech", "嘴型同步视频", ["speech-model"]],
].map(([key, label, ids]) => [key, { label, default: ids[0], options: ids.map(id => ({ id, label: id, description: `${label}能力` })) }])) };
const clone = value => JSON.parse(JSON.stringify(value));
const settle = () => new Promise(resolve => setImmediate(resolve));
const Empty = () => null, Button = () => null, Selector = () => null, Usage = () => null;
const MediaImage = () => null;
function text(node) { return Array.isArray(node) ? node.map(text).join("") : node && typeof node === "object" ? text(node.props?.children) : node == null ? "" : String(node); }
function nodes(tree, predicate) { const found = []; function walk(node) { if (Array.isArray(node)) return node.forEach(walk); if (!node || typeof node !== "object") return; if (predicate(node)) found.push(node); walk(node.props?.children); } walk(tree); return found; }
function compile(file, modules, globals = {}) {
  const source = ts.transpileModule(fs.readFileSync(new URL(`../${file}`, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
  const exports = {};
  vm.runInNewContext(source, { exports, Error, AbortController, ...globals, require: id => modules[id] ?? { CreationForm: Empty, ScriptView: Empty, StageView: Empty, ArtifactEditor: Empty, ComicStageView: Empty, ComicStoryboardEditor: Empty, KnowledgeSources: Empty, SessionKnowledge: Empty, KnowledgeLibraryPicker: Empty, AssetPicker: Empty } });
  return exports;
}
function view(file, name, props, { states = [], modules = {}, modelState = { catalog, loading: false, error: "", retry: () => {} } } = {}) {
  let stateIndex = 0, refIndex = 0, uuid = 0;
  const refs = [], stored = new Map();
  const exports = compile(file, {
    react: { useState: initial => { const index = stateIndex++; if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial; return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }]; }, useRef: initial => refs[refIndex++] ?? (refs[refIndex - 1] = { current: initial }), useCallback: fn => fn, useEffect: () => {} },
    "react/jsx-runtime": require("react/jsx-runtime"), "lucide-react": { ArrowUpRight: Empty, SlidersHorizontal: Empty, ChevronDown: Empty, Cpu: Empty },
    "@/components/ui/Button": { Button }, "@/components/ui/Input": { Input: Empty }, "@/lib/workflow": workflow,
    "@/lib/useModelCatalog": { useModelCatalog: () => modelState }, "@/features/cinema/SessionLibrary": { STAGE_CN: { script_generation: "剧本", character_design: "角色", video_generation: "视频" }, TaskVideo: Empty },
    "./ModelSelector": { ModelSelector: Selector, ModelUsageNote: Usage }, "@/features/creation/ModelSelector": { ModelSelector: Selector, ModelUsageNote: Usage }, ...modules,
    "./media": { MediaImage },
  }, { localStorage: { getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key) }, crypto: { randomUUID: () => `submission-${++uuid}` } });
  return { states, stored, render: () => { stateIndex = 0; refIndex = 0; return exports[name](props); } };
}
function fixture(overrides = {}) {
  return { session_id: "owned-session", project_type: "story", status: "idle", current_stage: "script_generation", video_generation_mode: "first_frame", artifacts: {}, stages_completed: [], stale_stages: [], model_selection: { text: "qwen3.5-plus", image: "qwen-image-2.0" }, ...overrides };
}
function panel(session, api = {}, modelState) {
  let backend = session;
  const calls = [];
  const apiModule = {
    getSession: async () => backend,
    updateSessionModels: async (id, selection) => { calls.push(["patch", id, selection]); backend = { ...backend, model_selection: selection }; return backend; },
    executeStage: async () => { calls.push(["execute", clone(backend.model_selection)]); },
    interveneSession: async (id, stage, modifications) => { calls.push(["intervene", id, stage, modifications, clone(backend.model_selection)]); },
    ...api,
  };
  const harness = view("features/creation/CreationPanel.tsx", "CreationPanel", { sessionId: session.session_id, userId: "a", onCreated: () => {}, onChanged: () => {} }, { states: [session, false, null, "", { message: "", percent: 0 }, "", "", false], modules: { "@/lib/api/sessions": apiModule, "@/lib/media": {} }, ...(modelState ? { modelState } : {}) });
  return { ...harness, calls };
}

function referenceVideoSession(overrides = {}) {
  return fixture({ current_stage: "video_generation", stages_completed: workflow.PROJECT_STAGES.story.slice(0, 4),
    artifacts: { reference_generation: { shots: [{ shot_id: "s1", path: "/old.png", selected: "/selected.png" }, { shot_id: "s2", path: "/two.png" }] }, video_generation: { stale_items: ["s1", "s2"] } },
    stale_stages: ["video_generation", "post_production"], ...overrides });
}

function videoCandidateCatalog() {
  const value = clone(catalog);
  value.groups.video_reference.options.push({ id: "veo-3.1-generate-preview", label: "Veo 3.1", description: "8秒；最多3张参考图", video: { duration_seconds: 8, ratios: ["16:9", "9:16"], resolutions: ["720P", "1080P"], max_reference_images: 3, estimated_clip_cny: { "720P": 25.6, "1080P": 25.6 } } });
  return value;
}

test("video candidate displays duration and per-clip estimates without a generation request", () => {
  const changes = [], value = videoCandidateCatalog();
  const screen = view("features/creation/ModelSelector.tsx", "ModelSelector", { catalog: value, loading: false, error: "", keys: ["video_reference"], selected: { video_reference: "veo-3.1-generate-preview" }, onChange: v => changes.push(v), onRetry: () => {} });
  assert.match(text(screen.render()), /8秒\/片段/);
  assert.match(text(screen.render()), /720P约¥25\.60\/片段/);
  nodes(screen.render(), n => n.type === "select")[0].props.onChange({ target: { value: "wan2.7-r2v" } });
  assert.equal(changes[0].video_reference, "wan2.7-r2v");
});

test("creation form explains incompatible video format before creating a paid workflow", () => {
  const value = videoCandidateCatalog();
  value.groups.video_first_frame.options.push(value.groups.video_reference.options.at(-1));
  const screen = view("features/creation/CreationForm.tsx", "CreationForm", {
    draft: { idea: "人物吃炸鸡", style: "realistic", episodes: 1, ratio: "1:1", knowledgeLibraryIds: [], modelSelection: { video_first_frame: "veo-3.1-generate-preview" } },
    onDraftChange: () => {}, onCreated: () => {}, projectType: "story",
  }, { modelState: { catalog: value, loading: false, error: "", retry: () => {} } });
  assert.match(text(screen.render()), /当前作品为 1:1 720P/);
  assert.ok(nodes(screen.render(), n => n.type === Button && n.props.disabled).length);
});

test("incompatible Veo square format blocks generation and compatible format is usable", async () => {
  const value = videoCandidateCatalog();
  const screen = panel(referenceVideoSession({ video_generation_mode: "reference", video_ratio: "1:1", resolution: "720P", model_selection: { video_reference: "veo-3.1-generate-preview" } }), {}, { catalog: value, loading: false, error: "", retry: () => {} });
  assert.match(text(screen.render()), /当前作品为 1:1 720P/);
  const button = nodes(screen.render(), n => n.type === Button && text(n) === "使用 2 张参考图生成视频")[0];
  assert.equal(button.props.disabled, true);
  button.props.onClick(); await settle();
  assert.deepEqual(screen.calls, []);
  screen.states[0] = { ...screen.states[0], video_ratio: "16:9" };
  assert.equal(nodes(screen.render(), n => n.type === Button && text(n) === "使用 2 张参考图生成视频")[0].props.disabled, false);
});

test("confirming reference images enters video through the server without starting a paid generation", async () => {
  const session = referenceVideoSession({ current_stage: "reference_generation", status: "stage_completed" });
  const next = { ...session, current_stage: "video_generation", status: "idle" };
  let confirmed = 0;
  const screen = panel(session, { continueSession: async () => { confirmed++; return next; } });
  const button = nodes(screen.render(), n => n.type === Button && text(n) === "确认参考图，进入视频生成")[0];
  assert.equal(button.props.disabled, false);
  button.props.onClick(); button.props.onClick(); await settle();
  assert.equal(confirmed, 1);
  assert.equal(screen.states[0].current_stage, "video_generation");
  assert.deepEqual(screen.calls, []);
});

test("returning to completed references retains a direct navigation button to video", () => {
  const screen = panel(referenceVideoSession());
  screen.states[2] = "reference_generation";
  const button = nodes(screen.render(), n => n.type === Button && text(n) === "前往视频生成")[0];
  assert.equal(button.props.disabled, false);
  button.props.onClick();
  assert.equal(screen.states[2], "video_generation");
  assert.deepEqual(screen.calls, []);
});

test("video shows the selected reference for each shot and generates through the existing stage endpoint", async () => {
  const screen = panel(referenceVideoSession());
  const tree = screen.render();
  assert.deepEqual(nodes(tree, n => n.type === MediaImage).map(n => n.props.path), ["/selected.png", "/two.png"]);
  assert.match(text(tree), /镜头 1 → 片段 1/);
  assert.doesNotMatch(text(tree), /重新生成本阶段全部视频/);
  const button = nodes(tree, n => n.type === Button && text(n) === "使用 2 张参考图生成视频")[0];
  assert.equal(button.props.disabled, false);
  button.props.onClick(); await settle();
  assert.equal(screen.calls.filter(c => c[0] === "intervene").length, 1);
  assert.equal(screen.calls.find(c => c[0] === "intervene")[2], "video_generation");
});

test("missing or stale references cannot start video generation", async () => {
  for (const overrides of [{ artifacts: {} }, { stale_stages: ["reference_generation", "video_generation"] }]) {
    const screen = panel(referenceVideoSession(overrides));
    const button = nodes(screen.render(), n => n.type === Button && /^使用 .*张参考图生成视频$/.test(text(n)))[0];
    assert.equal(button.props.disabled, true);
    button.props.onClick(); await settle();
    assert.deepEqual(screen.calls, []);
  }
});

test("an active video shows elapsed waiting and saved clips even without backend heartbeat events", () => {
  const session = referenceVideoSession({ status: "running", execution: { status: "running", stage: "video_generation", updated_at: 1000, last_event: { message: "正在生成视频片段 s2", percent: 30 } } });
  session.artifacts.video_generation.segments = [{ shot_id: "s1", path: "/first.mp4" }];
  const screen = panel(session);
  screen.states[8] = 1065000;
  const rendered = text(screen.render());
  assert.match(rendered, /此步骤已等待 65 秒/);
  assert.match(rendered, /已完成 1\/2 个片段/);
  assert.match(rendered, /30%/);
  assert.deepEqual(screen.calls, []);
});

test("model preferences migrate without replacing legacy stories and remain isolated by account, project and tool", () => {
  const saved = workflow.emptyDraft(); saved.story.idea = "雨夜女孩"; saved.story.episodes = 4; saved.story.ratio = "16:9";
  const storage = new Map([[workflow.draftKey("a"), JSON.stringify(saved)]]), store = { getItem: key => storage.get(key) ?? null };
  assert.equal(workflow.readDraft(store, "a").story.modelSelection, undefined);
  saved.story.modelSelection = { text: "qwen3.5-flash", image: "retired-model" }; saved.comic.modelSelection = { image: "qwen-image-2.0-pro", video_reference: "wan2.7-r2v" };
  saved.tools.literary.modelSelection = { text: "qwen3.5-plus" }; saved.tools.motion.modelSelection = { video_reference: "wan2.7-r2v" };
  storage.set(workflow.draftKey("a"), JSON.stringify(saved));
  const restored = workflow.readDraft(store, "a");
  assert.equal(restored.story.idea, "雨夜女孩"); assert.equal(restored.story.episodes, 4); assert.equal(restored.story.ratio, "16:9");
  assert.deepEqual(restored.story.modelSelection, saved.story.modelSelection); assert.deepEqual(restored.comic.modelSelection, { image: "qwen-image-2.0-pro" });
  assert.deepEqual(restored.tools.motion.modelSelection, { video_reference: "wan2.7-r2v" }); assert.deepEqual(workflow.readDraft(store, "b"), workflow.emptyDraft());
});

test("an explicit four-episode selection in the form survives legacy draft migration", () => {
  let changed;
  const draft = { idea: "故事", style: "realistic", episodes: 1, ratio: "16:9", knowledgeLibraryIds: [] };
  const harness = view("features/creation/CreationForm.tsx", "CreationForm", {
    draft, onDraftChange: value => { changed = value; }, onCreated: () => {}, projectType: "story",
  });
  const input = nodes(harness.render(), node => node.props?.id === "episodes")[0];
  input.props.onChange({ target: { value: "4" } });
  const restored = workflow.readDraft({ getItem: () => JSON.stringify({ ...workflow.emptyDraft(), story: changed }) }, "a");
  assert.equal(restored.story.episodes, 4);
  assert.equal(workflow.creationInput(restored.story, "story").episodes, 4);
});

test("model scopes match real text, image and video calls, including voice and composition stages with no generated video", () => {
  assert.deepEqual(workflow.creationModelKeys("comic"), ["text", "image"]);
  assert.deepEqual(workflow.stageModelKeys("video_generation", "reference"), ["video_reference"]);
  assert.deepEqual(workflow.stageModelKeys("video_generation", "start_end"), ["video_start_end"]);
  assert.deepEqual(workflow.stageModelKeys("video_generation", "first_frame"), ["video_first_frame"]);
  for (const stage of ["comic_audio", "comic_composition", "post_production"]) assert.deepEqual(workflow.stageModelKeys(stage, "first_frame"), []);
  assert.deepEqual(workflow.toolModelKeys("literary"), ["text", "image"]); assert.deepEqual(workflow.toolModelKeys("motion"), ["video_reference"]); assert.deepEqual(workflow.toolModelKeys("talking"), []);
  assert.deepEqual(workflow.toolModelKeys("talking", "lip_sync"), ["video_speech"]); assert.deepEqual(workflow.toolModelKeys("talking", "static"), []);
  const draft = { ...workflow.emptyDraft().comic, modelSelection: { text: "qwen3.5-flash", image: "qwen-image-2.0-pro", video_first_frame: "wan2.6-i2v" } };
  assert.deepEqual(workflow.creationInput(draft, "comic").model_selection, { text: "qwen3.5-flash", image: "qwen-image-2.0-pro" });
});

test("older model catalogs keep existing tools available while speech stays closed", async () => {
  const legacy = clone(catalog); delete legacy.groups.video_speech;
  const models = compile("lib/api/models.ts", { "./client": { api: async () => legacy } });
  const loaded = await models.getModelCatalog();
  assert.deepEqual(clone(loaded.groups.video_speech), { label: "嘴型同步视频", default: null, options: [] });
  assert.match(workflow.modelSelectionError({}, loaded, ["video_speech"]), /嘴型同步视频模型暂未开放/);
  assert.equal(workflow.modelSelectionError({ text: "qwen3.5-plus" }, loaded, ["text"]), "");
});

test("speaking form sends speech video models only in lip sync mode and mode changes create a new retry key", async () => {
  const attempts = [], props = { tab: "talking", userId: "a", draft: { ...workflow.emptyDraft().tools.talking, text: "你好", image: "person.png" }, onTabChange: () => {}, onOpenTask: () => {}, onCreated: () => {} };
  props.onDraftChange = next => { props.draft = next; };
  const screen = view("features/pipelines/PipelinesPanel.tsx", "PipelinesPanel", props, { modules: { "@/lib/api/tasks": { createTask: async (type, input, key) => { attempts.push({ type, input, key }); throw new Error("提交中断，输入已保留"); } } } });
  const submit = () => nodes(screen.render(), n => n.type === Button && text(n) === "生成快捷短片")[0].props.onClick();
  assert.deepEqual(nodes(screen.render(), n => n.type === Selector)[0].props.keys, ["video_speech"]);
  submit(); await settle();
  assert.equal(attempts[0].input.talking_mode, "lip_sync"); assert.deepEqual(clone(attempts[0].input.model_selection), { video_speech: "speech-model" });
  assert.equal(props.draft.text, "你好"); assert.equal(props.draft.image, "person.png");
  nodes(screen.render(), n => n.type === "input" && n.props.value === "static")[0].props.onChange();
  assert.deepEqual(nodes(screen.render(), n => n.type === Selector)[0].props.keys, []);
  submit(); await settle(); assert.equal(attempts[1].input.talking_mode, "static"); assert.equal(attempts[1].input.model_selection, undefined); assert.notEqual(attempts[0].key, attempts[1].key);
});

test("speech mode validates non-whitespace character limits without truncating the saved script or static mode", async () => {
  let requests = 0;
  const props = { tab: "talking", userId: "a", draft: { ...workflow.emptyDraft().tools.talking, text: `${"你".repeat(39)} \n 😀`, image: "person.png" }, onTabChange: () => {}, onOpenTask: () => {}, onCreated: () => {} };
  props.onDraftChange = next => { props.draft = next; };
  const screen = view("features/pipelines/PipelinesPanel.tsx", "PipelinesPanel", props, { modules: { "@/lib/api/tasks": { createTask: async () => { requests++; throw new Error("测试到达提交"); } } } });
  const button = () => nodes(screen.render(), n => n.type === Button && text(n) === "生成快捷短片")[0];
  assert.equal(button().props.disabled, false); assert.match(text(screen.render()), /40\/40/);
  props.draft.text += "好"; assert.equal(button().props.disabled, true);
  button().props.onClick(); await settle(); assert.equal(requests, 0); assert.match(text(screen.render()), /最多 40 个非空白字符/);
  assert.equal(props.draft.text.endsWith("😀好"), true);
  nodes(screen.render(), n => n.type === "input" && n.props.value === "static")[0].props.onChange();
  assert.equal(button().props.disabled, false); button().props.onClick(); await settle(); assert.equal(requests, 1);
});

test("motion creation submits image and action description without letting a stale hidden video reference block generation", async () => {
  const attempts = [], props = { tab: "motion", userId: "a", draft: { ...workflow.emptyDraft().tools.motion, text: "人物微笑挥手", image: "person.png", motion: "deleted-reference.mp4", motionName: "历史动作.mp4" }, onTabChange: () => {}, onOpenTask: () => {}, onCreated: () => {} };
  props.onDraftChange = next => { props.draft = next; };
  const screen = view("features/pipelines/PipelinesPanel.tsx", "PipelinesPanel", props, { modules: { "@/lib/api/tasks": { createTask: async (type, input) => { if ("motion_video" in input) throw new Error("旧视频不存在"); attempts.push({ type, input }); return { task_id: "new-motion" }; }, listTasks: async () => [] } } });
  const tree = screen.render(); assert.equal(nodes(tree, n => n.type === "input" && n.props.accept === "video/*").length, 0);
  nodes(tree, n => n.type === Button && text(n) === "生成快捷短片")[0].props.onClick(); await settle();
  assert.equal(attempts.length, 1); assert.equal(attempts[0].input.character_image, "person.png"); assert.equal(attempts[0].input.prompt, "人物微笑挥手"); assert.equal("motion_video" in attempts[0].input, false);
  assert.equal("motion_video" in workflow.toolTaskInput("motion", { ...props.draft, motion: "" }), false);
  assert.equal(props.draft.motion, "deleted-reference.mp4"); assert.equal(props.draft.motionName, "历史动作.mp4");
  assert.match(text(screen.render()), /已提交/);
});

test("unavailable speech model blocks lip sync generation but leaves explicit static mode usable", async () => {
  let requests = 0;
  const closed = clone(catalog); closed.groups.video_speech = { label: "嘴型同步视频", default: null, options: [] };
  const props = { tab: "talking", userId: "a", draft: { ...workflow.emptyDraft().tools.talking, text: "你好", image: "person.png" }, onTabChange: () => {}, onOpenTask: () => {}, onCreated: () => {} };
  props.onDraftChange = next => { props.draft = next; };
  const screen = view("features/pipelines/PipelinesPanel.tsx", "PipelinesPanel", props, { modelState: { catalog: closed, loading: false, error: "", retry: () => {} }, modules: { "@/lib/api/tasks": { createTask: async () => { requests++; throw new Error("测试到达提交"); } } } });
  const button = () => nodes(screen.render(), n => n.type === Button && text(n) === "生成快捷短片")[0];
  assert.equal(button().props.disabled, true); button().props.onClick(); await settle(); assert.equal(requests, 0); assert.match(text(screen.render()), /嘴型同步视频模型暂未开放/);
  nodes(screen.render(), n => n.type === "input" && n.props.value === "static")[0].props.onChange();
  assert.equal(button().props.disabled, false); button().props.onClick(); await settle(); assert.equal(requests, 1);
});

test("speaking detail never previews or offers download for mismatched completed output", () => {
  const task = { task_id: "speech", type: "talking_head", input: { talking_mode: "lip_sync", script: "你好" }, result: { talking_mode: "static" }, status: "completed", updated_at: 1 };
  const screen = view("features/pipelines/PipelinesPanel.tsx", "PipelinesPanel", { tab: "talking", taskId: "speech", userId: "a", draft: workflow.emptyDraft().tools.talking, onDraftChange: () => {}, onTabChange: () => {}, onOpenTask: () => {}, onCreated: () => {} }, { states: [[task], task] });
  const tree = screen.render(); assert.match(text(tree), /生成模式：人物嘴型同步/); assert.match(text(tree), /结果异常/); assert.match(text(tree), /未取得人物嘴型同步成片/);
  assert.equal(nodes(tree, n => n.type === Button && text(n) === "下载成片").length, 0);
  assert.equal(nodes(tree, n => n.props.taskId === "speech").length, 0);
});

test("catalog recommendations fill only unset preferences; retired and unavailable models never silently fall back", () => {
  const choice = workflow.resolvedModelSelection({ text: "retired" }, catalog, ["text", "image"]);
  assert.deepEqual(choice, { text: "retired", image: "qwen-image-2.0" }); assert.match(workflow.modelSelectionError(choice, catalog, ["text"]), /retired.*不可用/);
  const closed = clone(catalog); closed.groups.video_reference = { label: "参考图视频", default: null, options: [] };
  assert.deepEqual(workflow.resolvedModelSelection({}, closed, ["video_reference"]), {}); assert.match(workflow.modelSelectionError({}, closed, ["video_reference"]), /暂未开放/);
  assert.equal(workflow.modelSelectionError({ text: "qwen3.5-plus" }, closed, ["text"]), "");
  const noDefault = clone(catalog); noDefault.groups.text.default = null;
  assert.match(workflow.modelSelectionError(workflow.resolvedModelSelection({}, noDefault, ["text"]), noDefault, ["text"]), /请选择/);
});

test("catalog and session model APIs use normal authenticated routes and preserve empty capability groups and server errors", async () => {
  const calls = [], closed = clone(catalog); closed.groups.video_start_end = { label: "首尾帧", default: null, options: [] };
  const api = async (path, options) => { calls.push([path, options]); return closed; };
  const models = compile("lib/api/models.ts", { "./client": { api } });
  assert.equal(await models.getModelCatalog(), closed); assert.equal(calls[0][0], "/api/models");
  const sessions = compile("lib/api/sessions.ts", { "./client": { api }, "../config": { API_BASE: "" }, "../stream": {} });
  await sessions.updateSessionModels("owned/id", { text: "qwen3.5-flash" });
  assert.equal(calls[1][0], "/api/sessions/owned%2Fid/models"); assert.equal(calls[1][1].method, "PATCH");
  assert.deepEqual(JSON.parse(calls[1][1].body), { model_selection: { text: "qwen3.5-flash" } });
  const rejected = compile("lib/api/models.ts", { "./client": { api: async () => { throw new Error("模型目录暂时不可用"); } } });
  await assert.rejects(rejected.getModelCatalog(), /暂时不可用/);
  const malformed = compile("lib/api/models.ts", { "./client": { api: async () => ({ provider: "AIHubMix", groups: {} }) } });
  await assert.rejects(malformed.getModelCatalog(), /目录不完整/);
});

test("actual creation form submits visible chosen models and preserves the draft after a create failure", async () => {
  const calls = [], props = { variant: "cinema", projectType: "comic", draft: { ...workflow.emptyDraft().comic, idea: "漫剧测试", orchestrationMode: "workflow", modelSelection: { text: "qwen3.5-flash", image: "qwen-image-2.0-pro" } }, onCreated: () => {} };
  props.onDraftChange = next => { props.draft = next; };
  const form = view("features/creation/CreationForm.tsx", "CreationForm", props, { modules: { "@/lib/api/sessions": { createSession: async input => { calls.push(input); throw new Error("创建失败，请重试"); } } } });
  const first = form.render(); assert.deepEqual(nodes(first, n => n.type === Selector)[0].props.keys, ["text", "image"]);
  await first.props.onSubmit({ preventDefault: () => {} });
  assert.deepEqual(clone(calls[0].model_selection), clone(props.draft.modelSelection)); assert.equal(calls[0].episodes, 1); assert.equal(calls[0].project_type, "comic");
  assert.equal(calls[0].orchestration_mode, "multi_agent");
  assert.equal(props.draft.idea, "漫剧测试"); assert.match(text(form.render()), /创建失败/);
});

test("actual creation form cannot submit during catalog failure or with a retired saved choice, and offers catalog retry", async () => {
  let requests = 0, retries = 0;
  const props = { variant: "cinema", draft: { ...workflow.emptyDraft().story, idea: "保留创意" }, onCreated: () => {} };
  const state = { catalog: null, loading: false, error: "无法读取模型目录", retry: () => { retries++; } };
  const form = view("features/creation/CreationForm.tsx", "CreationForm", props, { modelState: state, modules: { "@/lib/api/sessions": { createSession: async () => { requests++; } } } });
  let tree = form.render(); assert.equal(nodes(tree, n => n.type === "button" && n.props.type === "submit")[0].props.disabled, true);
  nodes(tree, n => n.type === Selector)[0].props.onRetry(); assert.equal(retries, 1);
  await tree.props.onSubmit({ preventDefault: () => {} }); assert.equal(requests, 0); assert.match(text(form.render()), /无法读取模型目录/);
  state.catalog = catalog; state.error = ""; props.draft.modelSelection = { text: "retired" };
  tree = form.render(); await tree.props.onSubmit({ preventDefault: () => {} }); assert.equal(requests, 0); assert.match(text(form.render()), /retired.*不可用/);
});

test("actual stage selector waits for server save before generation and preserves historical model usage", async () => {
  const oldUsage = { provider: "AIHubMix", models: { text: "qwen3.5-plus" } };
  const session = fixture({ artifacts: { script_generation: { model_usage: oldUsage } } });
  let finish, saved = session; const sequence = [];
  const screen = panel(session, {
    updateSessionModels: (id, choice) => { sequence.push("patch"); return new Promise(resolve => { finish = () => { saved = { ...session, model_selection: choice }; resolve(saved); }; }); },
    getSession: async () => saved,
    interveneSession: async () => { sequence.push("generate"); },
  });
  let tree = screen.render(); const before = nodes(tree, n => n.type === Button)[0];
  nodes(tree, n => n.type === Selector)[0].props.onChange({ text: "qwen3.5-flash" });
  before.props.onClick(); assert.deepEqual(sequence, ["patch"]); assert.equal(nodes(screen.render(), n => n.type === Selector)[0].props.disabled, true);
  finish(); await settle(); tree = screen.render();
  assert.equal(nodes(tree, n => n.type === Selector)[0].props.selected.text, "qwen3.5-flash"); assert.equal(saved.artifacts.script_generation.model_usage, oldUsage);
  nodes(tree, n => n.type === Button)[0].props.onClick(); await settle(); assert.deepEqual(sequence, ["patch", "generate"]);
});

test("legacy stage default must be saved successfully before execute; a rejected save leaves the artifact and choice unchanged", async () => {
  const session = fixture(); delete session.model_selection;
  let executes = 0;
  const screen = panel(session, { updateSessionModels: async () => { throw new Error("模型保存失败，请重试"); }, executeStage: async () => { executes++; } });
  nodes(screen.render(), n => n.type === Button)[0].props.onClick(); await settle();
  assert.equal(executes, 0); assert.equal(screen.states[0], session); assert.match(text(screen.render()), /模型保存失败/);
  assert.equal(nodes(screen.render(), n => n.type === Selector)[0].props.disabled, false);
});

test("server-running and pending stages disable model changes even when a saved local callback is invoked", async () => {
  for (const status of ["pending", "running"]) {
    let patches = 0;
    const screen = panel(fixture({ execution: { status } }), { updateSessionModels: async () => { patches++; } });
    const selector = nodes(screen.render(), n => n.type === Selector)[0]; assert.equal(selector.props.disabled, true);
    selector.props.onChange({ text: "qwen3.5-flash" }); await settle(); assert.equal(patches, 0);
  }
});

test("actual quick-tool submission includes chosen models in stable retries and restores historical choices", async () => {
  const attempts = [], props = { tab: "literary", userId: "a", draft: { ...workflow.emptyDraft().tools.literary, text: "月光落在车站", modelSelection: { text: "qwen3.5-plus", image: "qwen-image-2.0" } }, onTabChange: () => {}, onOpenTask: () => {}, onCreated: () => {} };
  props.onDraftChange = next => { props.draft = next; };
  const screen = view("features/pipelines/PipelinesPanel.tsx", "PipelinesPanel", props, { modules: { "@/lib/api/tasks": { createTask: async (type, input, key) => { attempts.push({ type, input, key }); throw new Error("网络中断，请重试"); } } } });
  const submit = () => nodes(screen.render(), n => n.type === Button && text(n) === "生成快捷短片")[0].props.onClick();
  submit(); await settle(); submit(); await settle(); assert.equal(attempts[0].key, attempts[1].key);
  assert.deepEqual(clone(attempts[0].input.model_selection), clone(props.draft.modelSelection));
  nodes(screen.render(), n => n.type === Selector)[0].props.onChange({ text: "qwen3.5-flash", image: "qwen-image-2.0" });
  submit(); await settle(); assert.notEqual(attempts[2].key, attempts[1].key); assert.equal(attempts[2].input.model_selection.text, "qwen3.5-flash");
  const restored = workflow.toolDraftFromTask({ type: "motion_transfer", input: { character_image: "/uploads/person.png", motion_video: "/uploads/action.mp4", prompt: "动作", model_selection: { video_reference: "wan2.7-r2v" } } });
  assert.equal(restored.image, "person.png"); assert.deepEqual(restored.modelSelection, { video_reference: "wan2.7-r2v" });
  assert.equal(workflow.toolTaskInput("talking", { ...restored, text: "口播" }).model_selection, undefined);
});

test("actual model widget exposes only server candidates and keeps invalid preferences visible until explicit correction", () => {
  const changes = [], retry = () => {};
  const widget = view("features/creation/ModelSelector.tsx", "ModelSelector", { catalog, loading: false, error: "", keys: ["text"], selected: { text: "retired" }, onChange: value => changes.push(value), onRetry: retry });
  const tree = widget.render(), select = nodes(tree, n => n.type === "select")[0];
  assert.equal(select.props.value, "retired"); assert.match(text(tree), /retired.*不可用/);
  assert.deepEqual(nodes(tree, n => n.type === "option").map(n => n.props.value), ["retired", "qwen3.5-plus", "qwen3.5-flash"]);
  select.props.onChange({ target: { value: "qwen3.5-flash" } }); assert.equal(changes[0].text, "qwen3.5-flash");
});

test("mixed model metadata remains stored without exposing source annotations", () => {
  const sources = ["qwen-image-2.0", "qwen-image-2.0-pro"].map(image => ({ provider: "AIHubMix", models: { image } }));
  const shots = sources.map((model_usage, i) => ({ shot_id: `s${i + 1}`, path: `shot-${i}.png`, selected: `shot-${i}.png`, versions: [`shot-${i}.png`], model_usage }));
  const props = { sessionId: "a", userId: "a", projectType: "story", stage: "reference_generation", artifact: { shots }, disabled: false, generationBlocked: false, stale: false, characters: [], onSelect: () => {}, onRegenerate: () => {} };
  const media = { MediaImage: Empty, MediaVideo: Empty, MediaAudio: Empty };
  const story = view("features/creation/StageView.tsx", "StageView", props, { modules: { "./media": media } });
  assert.equal(nodes(story.render(), n => n.type === Usage).length, 0);
  const comic = view("features/creation/ComicStageView.tsx", "ComicStageView", { ...props, stage: "comic_panels" }, { modules: { "./media": media, "@/lib/comic": {} } });
  assert.equal(nodes(comic.render(), n => n.type === Usage).length, 0);
});
