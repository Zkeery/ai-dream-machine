import assert from "node:assert/strict";
import test from "node:test";
import { emptyDraft, draftKey, readDraft, carryStoryToTool, parseRoute, routeUrl, libraryItems, taskCanPreview, taskResultError, taskIsActive, toolDraftFromTask, toolTaskInput, stageGenerationRequest, targetedGenerationRequest, pendingGenerationTargets, normalizeKnowledgeIds } from "../lib/workflow.ts";

function storage() {
  const rows = new Map();
  return { getItem: key => rows.get(key) ?? null, setItem: (key, value) => rows.set(key, value) };
}

test("a draft survives reload with all applicable fields and uploaded references, isolated by account", () => {
  const store = storage(), draft = emptyDraft();
  draft.story = { ...draft.story, idea: "雨夜旅人", style: "anime", episodes: 3, ratio: "9:16", knowledgeLibraryIds: ["world-a", "brand-a"] };
  draft.tool = "talking";
  draft.tools.talking = { ...draft.tools.talking, text: "口播草稿", image: "registered-image.png", imageName: "人物.png", assetSource: "asset-id" };
  store.setItem(draftKey("user-a"), JSON.stringify(draft));
  assert.deepEqual(readDraft(store, "user-a"), draft);
  assert.deepEqual(readDraft(store, "user-b"), emptyDraft());
});

test("invalid, older or partially corrupt storage recovers without leaking extra fields", () => {
  const store = storage();
  for (const value of ["broken", "null", JSON.stringify({ version: 0, story: { idea: "old" } })]) {
    store.setItem(draftKey("a"), value); assert.deepEqual(readDraft(store, "a"), emptyDraft());
  }
  store.setItem(draftKey("a"), JSON.stringify({ version: 1, tool: "constructor", story: { idea: "kept", episodes: "not a number" }, tools: { literary: { text: "kept too", image: null, token: "never copy" } } }));
  const restored = readDraft(store, "a");
  assert.equal(restored.tool, "literary"); assert.equal(restored.story.idea, "kept"); assert.equal(restored.story.episodes, 1);
  assert.equal(restored.tools.literary.text, "kept too"); assert.equal(restored.tools.literary.image, ""); assert.equal("token" in restored.tools.literary, false);
});

test("home handoff keeps literary text/style, preserves tool uploads, and excludes unsupported story fields", () => {
  const draft = emptyDraft(); draft.story = { idea: "开场文案", style: "anime", episodes: 3, ratio: "9:16", knowledgeLibraryIds: ["world-a"] };
  draft.tools.literary.image = "existing.png";
  const next = carryStoryToTool(draft, "literary");
  assert.equal(next.tools.literary.text, "开场文案"); assert.equal(next.tools.literary.style, "anime"); assert.equal(next.tools.literary.image, "existing.png");
  assert.equal("ratio" in next.tools.literary, false); assert.equal("episodes" in next.tools.literary, false);
  assert.equal("knowledgeLibraryIds" in next.tools.literary, false);
  assert.equal(draft.tools.literary.text, "");
  assert.equal(carryStoryToTool(draft, "motion").tools.motion.text, "开场文案");
});

test("navigation round trips exact project/tool/task contexts and never retains a story ID in tool routes", () => {
  for (const route of [{ view: "create", sessionId: "s 你好" }, { view: "pipelines", tool: "motion", taskId: "t 1" }, { view: "knowledge", libraryId: "库 1" }, { view: "settings" }, { view: "projects" }]) {
    assert.deepEqual(parseRoute(new URL(routeUrl(route), "http://local").searchParams), route);
  }
  assert.deepEqual(parseRoute(new URLSearchParams("session=old&view=pipelines&tool=talking&task=new")), { view: "pipelines", tool: "talking", taskId: "new" });
  assert.equal(parseRoute(new URLSearchParams("view=pipelines&tool=constructor"), "motion").tool, "motion");
  assert.deepEqual(parseRoute(new URLSearchParams("view=settings&session=old")), { view: "settings" });
});

test("legacy drafts stay unbound, while new knowledge selections normalize and persist only for their account", () => {
  const store = storage();
  store.setItem(draftKey("legacy"), JSON.stringify({ version: 1, story: { idea: "旧故事", style: "anime", episodes: 2, ratio: "9:16" } }));
  const restored = readDraft(store, "legacy");
  assert.equal(restored.story.idea, "旧故事"); assert.deepEqual(restored.story.knowledgeLibraryIds, []);
  assert.deepEqual(normalizeKnowledgeIds([" world ", "world", null, "", "characters", "brand", "fourth"]), ["world", "characters", "brand"]);
  assert.deepEqual(normalizeKnowledgeIds("world"), []);
  assert.deepEqual(parseRoute(new URLSearchParams("view=knowledge&library=own&session=old&task=old")), { view: "knowledge", libraryId: "own" });
});

test("opening a shortcut from an empty home does not erase its existing draft", () => {
  const draft = emptyDraft(); draft.tools.talking.text = "已写好的口播草稿";
  assert.equal(carryStoryToTool(draft, "talking").tools.talking.text, "已写好的口播草稿");
});

test("legacy sessions and all three tasks enter one library once, preserving IDs, source, state and update order", () => {
  const session = { session_id: "same", idea: "故事", artifacts: { script_generation: { title: "故事名称" } }, status: "stage_completed", updated_at: 10 };
  const tasks = [
    { task_id: "same", type: "literary_video", input: { text: "文学短片" }, status: "completed", updated_at: 11 },
    { task_id: "motion", type: "motion_transfer", input: { prompt: "奔跑" }, status: "running", updated_at: 12 },
    { task_id: "talking", type: "talking_head", input: { script: "你好" }, status: "failed", updated_at: 9 },
  ];
  const items = libraryItems([session], tasks);
  assert.equal(items.length, 4); assert.equal(new Set(items.map(i => i.key)).size, 4);
  assert.deepEqual(items.map(i => i.key), ["task:motion", "task:same", "session:same", "task:talking"]);
  assert.equal(items.find(i => i.kind === "session").title, "故事名称");
  assert.equal(items.find(i => i.id === "talking").source, "图片配音口播 · 静态图片配音");
  assert.equal(items.find(i => i.id === "motion").task, tasks[1]);
});

test("only persisted pending/running task facts keep execution controls blocked", () => {
  assert.equal(taskIsActive("pending"), true); assert.equal(taskIsActive("running"), true);
  for (const status of ["completed", "failed", "interrupted", "idle"]) assert.equal(taskIsActive(status), false);
});

test("continue talking and motion tasks converts resolved upload paths back to registered input filenames", () => {
  const talking = { input: { person_image: "/server/data/uploads/registered-person.png", script: "你好", asset_source: { asset_id: "original-role" } } };
  const talkingInput = toolTaskInput("talking", toolDraftFromTask(talking));
  assert.deepEqual(talkingInput, { person_image: "registered-person.png", script: "你好", talking_mode: "static", asset_source: { asset_id: "original-role" } });
  const motion = { input: { character_image: "/server/data/uploads/registered-role.png", motion_video: "/server/data/uploads/registered-motion.mp4", prompt: "奔跑" } };
  const restoredMotion = toolDraftFromTask(motion);
  assert.equal(restoredMotion.motion, "registered-motion.mp4");
  const motionInput = toolTaskInput("motion", restoredMotion);
  assert.deepEqual(motionInput, { character_image: "registered-role.png", prompt: "奔跑" });
  assert.equal(Object.values(motionInput).some(value => value.includes("/server")), false);
  // Restoring references does not bypass the server's require_upload_owner lookup.
  const registered = new Set(["registered-person.png", "registered-role.png", "registered-motion.mp4"]);
  for (const name of [talkingInput.person_image, motionInput.character_image]) assert.equal(registered.has(name), true);
});

test("new speaking drafts default to lip sync while legacy saved drafts and historical tasks stay static", () => {
  const store = storage(), draft = emptyDraft();
  assert.equal(draft.tools.talking.talkingMode, "lip_sync");
  draft.tools.talking.text = "已保存文案"; draft.tools.talking.image = "person.png";
  delete draft.tools.talking.talkingMode;
  store.setItem(draftKey("legacy"), JSON.stringify(draft));
  const restored = readDraft(store, "legacy");
  assert.equal(restored.tools.talking.talkingMode, "static"); assert.equal(restored.tools.talking.text, "已保存文案");
  assert.equal(restored.tools.talking.image, "person.png"); assert.equal(readDraft(store, "new-user").tools.talking.talkingMode, "lip_sync");
  assert.equal(toolDraftFromTask({ type: "talking_head", input: { script: "旧作品" } }).talkingMode, "static");
  draft.tools.talking.talkingMode = "lip_sync"; draft.tools.talking.modelSelection = { video_speech: "speech-model", video_reference: "reference-model" };
  store.setItem(draftKey("current"), JSON.stringify(draft));
  assert.deepEqual(readDraft(store, "current").tools.talking.modelSelection, { video_speech: "speech-model" });
});

test("speaking task modes preserve only applicable models and restore the recorded mode", () => {
  const draft = { ...emptyDraft().tools.talking, image: "person.png", text: "你好", modelSelection: { video_speech: "speech-model", image: "image-model", video_reference: "reference-model" } };
  const input = toolTaskInput("talking", draft);
  assert.equal(input.talking_mode, "lip_sync"); assert.deepEqual(input.model_selection, { video_speech: "speech-model" });
  const restored = toolDraftFromTask({ type: "talking_head", input });
  assert.equal(restored.talkingMode, "lip_sync"); assert.deepEqual(restored.modelSelection, { video_speech: "speech-model" });
  assert.equal(toolTaskInput("talking", { ...draft, talkingMode: "static" }).model_selection, undefined);
  assert.deepEqual(toolTaskInput("literary", draft).model_selection, { image: "image-model" });
  assert.deepEqual(toolTaskInput("motion", draft).model_selection, { video_reference: "reference-model" });
});

test("lip sync completion requires a matching result and never exposes static or missing results as success", () => {
  const task = { task_id: "speech", type: "talking_head", input: { talking_mode: "lip_sync", script: "你好" }, status: "completed", updated_at: 1 };
  for (const result of [null, {}, { talking_mode: "static" }]) {
    const invalid = { ...task, result };
    assert.equal(taskCanPreview(invalid), false); assert.match(taskResultError(invalid), /未取得人物嘴型同步成片/);
    const item = libraryItems([], [invalid])[0]; assert.equal(item.status, "failed"); assert.match(item.source, /人物嘴型同步/);
  }
  assert.equal(taskCanPreview({ ...task, result: { talking_mode: "lip_sync" } }), true);
  assert.equal(taskCanPreview({ ...task, status: "failed", result: { talking_mode: "lip_sync" } }), false);
  assert.equal(taskCanPreview({ ...task, input: {}, result: null }), true);
});

test("regenerating an existing stage in a finished story uses intervention instead of the new-stage endpoint", () => {
  for (const stage of ["script_generation", "character_design", "storyboard", "reference_generation", "post_production"]) assert.deepEqual(stageGenerationRequest(stage, { old: true }, "first_frame", "first_frame"), { kind: "intervene", modifications: { operation: "regenerate" } });
  assert.deepEqual(stageGenerationRequest("video_generation", { segments: [] }, "first_frame", "reference"), { kind: "intervene", modifications: { operation: "regenerate", video_generation_mode: "reference" } });
  assert.deepEqual(stageGenerationRequest("script_generation", undefined, "first_frame", "first_frame"), { kind: "execute" });
});

test("a role prompt regeneration targets exactly that role and carries edited text into both input and description", () => {
  assert.deepEqual(targetedGenerationRequest("character_design", ["c1"], ["c1", "l1"], "  短发、灰色外套的旅人  "), { operation: "regenerate", target_ids: ["c1"], prompts: { c1: "短发、灰色外套的旅人" }, descriptions: { c1: "短发、灰色外套的旅人" } });
  assert.throws(() => targetedGenerationRequest("character_design", ["c1"], ["c1"], " "));
  assert.throws(() => targetedGenerationRequest("character_design", ["c1"], ["c1"], "x".repeat(4001)));
});

test("partial reference and video updates include only selected shot IDs, deduplicate, and never widen to all shots", () => {
  const request = targetedGenerationRequest("reference_generation", ["shot2", "shot2", "shot3"], ["shot1", "shot2", "shot3"]);
  assert.deepEqual(request, { operation: "regenerate", target_ids: ["shot2", "shot3"] });
  // Video input targets are shot IDs; segment IDs are reserved for version selection.
  assert.deepEqual(targetedGenerationRequest("video_generation", ["shot2"], ["shot1", "shot2"]), { operation: "regenerate", target_ids: ["shot2"] });
  assert.throws(() => targetedGenerationRequest("video_generation", ["segment2"], ["shot1", "shot2"]));
  assert.throws(() => targetedGenerationRequest("reference_generation", [], ["shot1"]));
  assert.throws(() => targetedGenerationRequest("reference_generation", ["other"], ["shot1"]));
  assert.throws(() => targetedGenerationRequest("post_production", ["shot1"], ["shot1"]));
});

test("select pending updates follows server stale_items and preserves completed items outside the request", () => {
  const ids = ["s1", "s2", "s3"];
  const pending = pendingGenerationTargets({ stale_items: ["s2", "s2", "unknown", null] }, ids);
  assert.deepEqual(pending, ["s2"]);
  assert.deepEqual(targetedGenerationRequest("video_generation", pending, ids), { operation: "regenerate", target_ids: ["s2"] });
  assert.deepEqual(pendingGenerationTargets({ stale_items: ["c1"] }, ["c1", "l1"]), ["c1"]);
  assert.deepEqual(pendingGenerationTargets({ shots: [] }, ids), []);
  assert.deepEqual(pendingGenerationTargets(null, ids), []);
});
