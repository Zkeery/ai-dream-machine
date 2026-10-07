import assert from "node:assert/strict";
import test from "node:test";
import { emptyDraft, readDraft, draftKey, parseRoute, routeUrl, creationInput, libraryItems, libraryMatchesFilter, PROJECT_STAGES, projectStageState, finalVideoOf, targetedGenerationRequest, stageGenerationRequest } from "../lib/workflow.ts";
import { comicStoryboardSave } from "../lib/comic.ts";

test("legacy four-episode default migrates to one without losing story text, ratio or knowledge", () => {
  const original = { version: 1, story: { idea: "雨夜女孩", style: "realistic", episodes: 4, ratio: "16:9", knowledgeLibraryIds: ["old-knowledge"] } };
  const restored = readDraft({ getItem: () => JSON.stringify(original) }, "a");
  assert.deepEqual(restored.story, { ...original.story, episodes: 1, episodeDefaultVersion: 1, orchestrationMode: "workflow" });
  assert.equal(creationInput(restored.story, "story").episodes, 1);
  restored.story.episodes = 4;
  const reopened = readDraft({ getItem: () => JSON.stringify(restored) }, "a");
  assert.equal(reopened.story.episodes, 4);
  assert.equal(creationInput(reopened.story, "story").episodes, 4);
  assert.deepEqual(restored.comic, emptyDraft().comic);
  assert.notEqual(restored.story.knowledgeLibraryIds, restored.comic.knowledgeLibraryIds);
});

test("story and comic drafts persist independently per account, and comic cannot inherit a story episode count", () => {
  const value = emptyDraft(); value.story.idea = "电影草稿"; value.story.episodes = 4;
  value.comic = { idea: "漫剧草稿", style: "漫画", episodes: 1, ratio: "9:16", knowledgeLibraryIds: ["comic-world"] };
  const rows = new Map([[draftKey("a"), JSON.stringify(value)]]);
  const store = { getItem: key => rows.get(key) ?? null };
  assert.deepEqual(readDraft(store, "a"), { ...value, comic: { ...value.comic, orchestrationMode: "workflow" } });
  assert.deepEqual(readDraft(store, "b"), emptyDraft());
  const corrupt = readDraft({ getItem: () => JSON.stringify({ ...value, comic: { ...value.comic, episodes: 4 } }) }, "a");
  assert.equal(corrupt.comic.episodes, 1); assert.equal(corrupt.story.episodes, 4);
  assert.deepEqual(creationInput(value.comic, "comic"), { project_type: "comic", idea: "漫剧草稿", style: "漫画", episodes: 1, video_ratio: "9:16", knowledge_library_ids: ["comic-world"] });
  assert.equal(creationInput(value.story, "story").episodes, 4);
});

test("comic URL survives refresh and history; unrelated tools do not inherit the project type", () => {
  for (const route of [{ view: "create", projectType: "comic", sessionId: "漫画 1" }, { view: "create", projectType: "comic", sessionId: undefined }]) assert.deepEqual(parseRoute(new URL(routeUrl(route), "https://studio.test").searchParams), route);
  assert.deepEqual(parseRoute(new URLSearchParams("project=comic&view=pipelines&tool=talking")), { view: "pipelines", tool: "talking", taskId: undefined });
  assert.equal(parseRoute(new URLSearchParams("project=unknown")).projectType, undefined);
  assert.equal(routeUrl({ view: "create" }), "/");
});

test("library separates comic, legacy story and shortcuts, and reads the right MP4 artifact", () => {
  const sessions = [{ session_id: "old", idea: "故事", status: "idle", updated_at: 1, artifacts: { post_production: { final_video: "/video/story.mp4" } } }, { session_id: "comic", project_type: "comic", idea: "漫剧", status: "session_completed", updated_at: 3, artifacts: { comic_composition: { final_video: "/video/comic.mp4" } } }];
  const items = libraryItems(sessions, [{ task_id: "tool", type: "talking_head", input: {}, status: "completed", updated_at: 2 }]);
  assert.deepEqual(items.filter(i => libraryMatchesFilter(i, "comic")).map(i => i.id), ["comic"]);
  assert.deepEqual(items.filter(i => libraryMatchesFilter(i, "story")).map(i => i.id), ["old"]);
  assert.deepEqual(items.filter(i => libraryMatchesFilter(i, "task")).map(i => i.id), ["tool"]);
  assert.equal(finalVideoOf(sessions[0]), "/video/story.mp4"); assert.equal(finalVideoOf(sessions[1]), "/video/comic.mp4");
});

test("comic stages follow server status and pending items never become confirmed after a partial update", () => {
  const session = { project_type: "comic", status: "stage_completed", current_stage: "comic_panels", stages_completed: PROJECT_STAGES.comic.slice(0, 4), stale_stages: [] };
  const state = projectStageState(session);
  assert.deepEqual(state.stageOrder, ["script_generation", "character_design", "comic_storyboard", "comic_panels", "comic_audio", "comic_composition"]);
  assert.equal(state.canConfirm, true); assert.equal(state.finalStage, "comic_composition");
  const pending = { ...session, status: "idle", stale_stages: ["comic_panels"], artifacts: { comic_panels: { stale_items: ["s2"] } } };
  assert.equal(projectStageState(pending).canConfirm, false);
  assert.equal(projectStageState(pending, "comic_audio").blocked, true);
  assert.equal(projectStageState(session, "video_generation").stage, "comic_panels");
  assert.equal(projectStageState({ ...session, project_type: undefined, current_stage: "storyboard" }).finalStage, "post_production");
  assert.deepEqual(stageGenerationRequest("comic_composition", { final_video: "old.mp4" }, "", ""), { kind: "intervene", modifications: { operation: "regenerate" } });
});

const plan = () => ({ shots: [{ shot_id: "s1", episode_number: 1, description: "车站对话", prompt: "漫画风格的雨夜", character_ids: ["c1"], setting_ids: [], motion: "push_in", silent_duration: 3, dialogues: [{ line_id: "line1", speaker_id: "c1", text: "你好", emotion: "好奇" }] }], voice_map: { c1: "voice-a" } });
test("comic edits save exact dialogue, voice and camera plan without triggering generation", () => {
  const artifact = plan(); artifact.shots[0].dialogues[0].text = "你是十年前的我吗？"; artifact.shots[0].motion = "pan_left";
  const request = comicStoryboardSave(artifact, ["c1"], ["voice-a"]);
  assert.equal(request.operation, "save"); assert.equal(request.artifact, artifact);
  assert.equal(request.artifact.shots[0].dialogues[0].text, "你是十年前的我吗？");
  for (const mutate of [p => { p.shots[0].dialogues[0].speaker_id = "unknown"; }, p => { p.shots[0].dialogues[0].text = "x".repeat(401); }, p => { p.voice_map.c1 = "unknown"; }, p => { p.shots[0].motion = "unsupported"; }, p => { p.shots[0].silent_duration = 11; }, p => { p.shots[0].dialogues.push({ ...p.shots[0].dialogues[0] }); }]) { const invalid = plan(); mutate(invalid); assert.throws(() => comicStoryboardSave(invalid, ["c1"], ["voice-a"])); }
  const silent = plan(); silent.shots[0].dialogues = []; assert.equal(comicStoryboardSave(silent, ["c1"], ["voice-a"]).operation, "save");
});

test("comic panel and audio regeneration only targets explicitly selected shots", () => {
  for (const stage of ["comic_panels", "comic_audio"]) {
    assert.deepEqual(targetedGenerationRequest(stage, ["s2", "s2"], ["s1", "s2"]), { operation: "regenerate", target_ids: ["s2"] });
    assert.throws(() => targetedGenerationRequest(stage, ["line2"], ["s1", "s2"]));
    assert.throws(() => targetedGenerationRequest(stage, [], ["s1", "s2"]));
    assert.throws(() => targetedGenerationRequest(stage, ["s2"], ["s1", "s2"], "audio does not accept prompts"));
  }
});
