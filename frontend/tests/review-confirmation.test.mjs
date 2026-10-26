import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";
import * as workflow from "../lib/workflow.ts";
import * as demo from "../lib/demo.ts";

const require = createRequire(import.meta.url), ts = require("typescript");
const Empty = () => null, Button = () => null, ReviewCard = () => null, Image = () => null;
const ModelSelector = () => null, Knowledge = () => null, StageView = () => null;
const settle = () => new Promise(resolve => setImmediate(resolve));
class ApiError extends Error { constructor(code, message, status) { super(message); this.code = code; this.status = status; } }
function text(node) { return Array.isArray(node) ? node.map(text).join("") : node && typeof node === "object" ? text(node.props?.children) : node == null ? "" : String(node); }
function nodes(tree, predicate) { const found = []; function walk(node) { if (Array.isArray(node)) return node.forEach(walk); if (!node || typeof node !== "object") return; if (predicate(node)) found.push(node); walk(node.props?.children); } walk(tree); return found; }
function compile(file, modules = {}, globals = {}) {
  const source = ts.transpileModule(fs.readFileSync(new URL(`../${file}`, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
  const exports = {};
  vm.runInNewContext(source, { exports, Error, AbortController, Headers, TextDecoder, ...globals, require: id => modules[id] ?? (["./demo", "../demo", "@/lib/demo"].includes(id) ? demo : { CreationForm: Empty, ScriptView: Empty, StageView: Empty, ArtifactEditor: Empty, ComicStageView: Empty, ComicStoryboardEditor: Empty, RecoveryNotice: Empty }) });
  return exports;
}
function view(file, name, props, { states = [], modules = {}, runEffects = false } = {}) {
  const refs = [], effects = [];
  let stateIndex = 0, refIndex = 0, effectIndex = 0;
  const react = {
    useState(initial) { const index = stateIndex++; if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial; return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }]; },
    useRef(initial) { const index = refIndex++; return refs[index] ?? (refs[index] = { current: initial }); },
    useCallback: fn => fn,
    useEffect(callback, dependencies) {
      if (!runEffects) return;
      const index = effectIndex++, previous = effects[index];
      if (!previous || dependencies.some((value, i) => value !== previous.dependencies[i])) { previous?.cleanup?.(); effects[index] = { callback, dependencies }; }
    },
  };
  const exports = compile(file, {
    react, "react/jsx-runtime": require("react/jsx-runtime"), "@/components/ui/Button": { Button },
    "@/lib/workflow": workflow, "@/features/cinema/SessionLibrary": { STAGE_CN: { reference_generation: "参考图", video_generation: "视频" } },
    "./ReviewConfirmation": { ReviewConfirmation: ReviewCard }, "./media": { MediaImage: Image },
    "./ModelSelector": { ModelSelector }, "@/features/knowledge/SessionKnowledge": { SessionKnowledge: Knowledge },
    "./StageView": { StageView }, "@/features/trial/TrialProvider": { useTrial: () => ({ generationCompleted: () => {} }) },
    "@/lib/useModelCatalog": { useModelCatalog: () => ({ catalog: { groups: { image: { default: "qwen-image-2.0", label: "图片", options: [{ id: "qwen-image-2.0" }] } } }, loading: false, error: "", retry: () => {} }) },
    ...modules,
  }, { setTimeout: () => 1, clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {}, localStorage: { getItem: () => null } });
  return { states, render: () => { stateIndex = refIndex = effectIndex = 0; const tree = exports[name](props); for (const effect of effects) if (effect.callback) { effect.cleanup = effect.callback(); delete effect.callback; } return tree; }, unmount: () => effects.forEach(effect => effect.cleanup?.()) };
}
const pending = { review_id: "review/one", execution_id: "original-execution", stage: "reference_generation", reason: "人物符合设定，但构图需由创作者确认。\n保留全部审查理由。", image_url: "/api/sessions/story/media/image/original.png", status: "pending" };
function session(status = "awaiting_review") { return { session_id: "story", status, current_stage: "reference_generation", project_type: "story", artifacts: {}, stages_completed: ["script_generation", "character_design", "storyboard"], stale_stages: [], model_selection: { image: "qwen-image-2.0" }, video_generation_mode: "reference", updated_at: 1, execution: { execution_id: "original-execution", status }, pending_review: status === "awaiting_review" ? pending : null }; }
const panelProps = { sessionId: "story", userId: "user", onCreated: () => {}, onChanged: () => {} };

test("review APIs use owned encoded routes, true SSE and false JSON without ordinary generation", async () => {
  const reads = [], streams = [];
  const api = compile("lib/api/sessions.ts", { "./client": { ApiError, api: async (path, options) => { reads.push({ path, options }); return path.endsWith("/reviews") ? { session_id: "story/id", reviews: [{ review_id: "review/id" }] } : session("failed"); } }, "../stream": { streamSSE: async (path, options) => { streams.push({ path, options }); } } });
  const signal = new AbortController().signal;
  const history = await api.getSessionReviews("story/id");
  assert.equal(history.session_id, "story/id"); assert.equal(history.reviews[0].review_id, "review/id");
  await api.confirmSessionReview("story/id", "review/id", true, () => {}, signal);
  const stopped = await api.confirmSessionReview("story/id", "review/id", false, () => {}, signal);
  assert.equal(reads[0].path, "/api/sessions/story%2Fid/reviews");
  assert.equal(streams[0].path, "/api/sessions/story%2Fid/reviews/review%2Fid/confirm");
  assert.deepEqual(JSON.parse(streams[0].options.body), { accepted: true });
  assert.equal(streams[0].options.signal, signal);
  assert.equal(reads[1].path, streams[0].path);
  assert.deepEqual(JSON.parse(reads[1].options.body), { accepted: false });
  assert.equal(stopped.status, "failed");
  for (const id of ["", "   "]) await assert.rejects(api.confirmSessionReview("story", id, true, () => {}), error => error.code === "REVIEW_ID_REQUIRED");
  assert.equal(streams.length, 1); assert.equal(reads.length, 2);
});

test("review_required terminates the stream, releases its reader, and does not become an error", async () => {
  const seen = []; let cancelled = 0, reads = 0;
  const payload = new TextEncoder().encode(`data: ${JSON.stringify({ type: "review_required", session: session(), pending_review: pending })}\n\ndata: ${JSON.stringify({ type: "error", error: { code: "should-not-run", message: "不应被消费" } })}\n\n`);
  const stream = compile("lib/stream.ts", { "./api/client": { ApiError, authHeaders: () => ({ Authorization: "Bearer mock-only" }) }, "./config": { API_BASE: "" } }, { fetch: async () => ({ ok: true, headers: new Headers({ "content-type": "text/event-stream" }), body: { getReader: () => ({ read: async () => { reads++; return { done: false, value: payload }; }, cancel: async () => { cancelled++; } }) } }) });
  await stream.streamSSE("/mock", { method: "POST" }, ev => seen.push(ev.type));
  assert.deepEqual(seen, ["review_required"]); assert.equal(cancelled, 1); assert.equal(reads, 1);
});

test("the review card renders the owned original image and full reason, with no automatic decision", () => {
  const decisions = [], reason = `${pending.reason}\n${"完整理由".repeat(120)}`;
  const screen = view("features/creation/ReviewConfirmation.tsx", "ReviewConfirmation", { sessionId: "story", review: { ...pending, reason }, busy: false, onDecision: async value => { decisions.push(value); } });
  const tree = screen.render();
  assert.equal(decisions.length, 0); assert.ok(text(tree).includes(reason));
  assert.equal(nodes(tree, node => node.type === Image)[0].props.path, pending.image_url);
  assert.equal(nodes(tree, node => node.type === Image)[0].props.sessionId, "story");
  assert.deepEqual(nodes(tree, node => node.type === Button).map(text), ["符合，继续", "不符合，停止"]);
  const acceptBeforeLoad = nodes(tree, node => node.type === Button)[0];
  assert.equal(acceptBeforeLoad.props.disabled, true); acceptBeforeLoad.props.onClick(); assert.equal(decisions.length, 0);
  nodes(tree, node => node.type === Image)[0].props.onLoad();
  assert.equal(nodes(screen.render(), node => node.type === Button)[0].props.disabled, false);
  assert.equal(decisions.length, 0);
  for (const debug of ["review/one", "original-execution", "prompt", "raw_content", "Agent"]) assert.equal(text(tree).includes(debug), false);
});

test("missing reason is explicit and incomplete or non-pending records never submit a decision", () => {
  const blank = view("features/creation/ReviewConfirmation.tsx", "ReviewConfirmation", { sessionId: "story", review: { ...pending, reason: " " }, busy: false, onDecision: async () => {} });
  assert.match(text(blank.render()), /未给出理由/);
  for (const review of [null, { ...pending, review_id: "" }, { ...pending, execution_id: "" }, { ...pending, image_url: "" }, { ...pending, status: "rejected" }]) {
    let decisions = 0;
    const screen = view("features/creation/ReviewConfirmation.tsx", "ReviewConfirmation", { sessionId: "story", review, busy: false, onDecision: async () => { decisions++; } });
    const buttons = nodes(screen.render(), node => node.type === Button);
    for (const button of buttons) { assert.equal(button.props.disabled, true); button.props.onClick(); }
    assert.equal(decisions, 0);
  }
});

test("reloading awaiting_review restores the card, clears busy, and locks mutations", async () => {
  const saved = { ...session(), execution: { status: "running", execution_id: "original-execution" } };
  const calls = [];
  const screen = view("features/creation/CreationPanel.tsx", "CreationPanel", panelProps, { runEffects: true, modules: { "@/lib/api/sessions": { getSession: async () => { calls.push("read"); return saved; } } } });
  screen.render(); await settle(); const tree = screen.render();
  const card = nodes(tree, node => node.type === ReviewCard)[0];
  assert.equal(card.props.review, pending); assert.equal(card.props.busy, false);
  assert.match(text(tree), /等待图片确认/); assert.doesNotMatch(text(tree), /点击生成|正在生成|生成失败/);
  assert.equal(nodes(tree, node => node.type === ModelSelector)[0].props.disabled, true);
  assert.equal(nodes(tree, node => node.type === Knowledge)[0].props.disabled, true);
  assert.ok(calls.every(call => call === "read")); screen.unmount();
});

test("manual acceptance has an immediate double-click lock and continues only the review route", async () => {
  const calls = []; let finish;
  const completed = session("stage_completed");
  const screen = view("features/creation/CreationPanel.tsx", "CreationPanel", panelProps, { states: [session()], modules: { "@/lib/api/sessions": {
    getSession: async () => completed,
    confirmSessionReview: async (id, reviewId, accepted, event) => { calls.push([id, reviewId, accepted]); await new Promise(resolve => { finish = () => { event({ type: "done", session: completed }); resolve(); }; }); },
    executeStage: async () => { calls.push("generate"); }, interveneSession: async () => { calls.push("intervene"); }, updateSessionModels: async () => { calls.push("models"); },
  } } });
  const card = nodes(screen.render(), node => node.type === ReviewCard)[0];
  assert.equal(calls.length, 0);
  const first = card.props.onDecision(true), second = card.props.onDecision(true);
  assert.deepEqual(calls, [["story", "review/one", true]]);
  assert.equal(nodes(screen.render(), node => node.type === ReviewCard)[0].props.busy, true);
  finish(); await Promise.all([first, second]);
  assert.equal(screen.states[0].status, "stage_completed"); assert.equal(screen.states[1], false);
  assert.equal(nodes(screen.render(), node => node.type === ReviewCard).length, 0);
});

test("manual rejection stops and never invokes a generation or continuation", async () => {
  const calls = [], stopped = session("failed");
  const screen = view("features/creation/CreationPanel.tsx", "CreationPanel", panelProps, { states: [session()], modules: { "@/lib/api/sessions": {
    getSession: async () => stopped,
    confirmSessionReview: async (id, reviewId, accepted) => { calls.push([id, reviewId, accepted]); return stopped; },
    executeStage: async () => { calls.push("generate"); }, continueSession: async () => { calls.push("continue"); },
  } } });
  await nodes(screen.render(), node => node.type === ReviewCard)[0].props.onDecision(false);
  assert.deepEqual(calls, [["story", "review/one", false]]);
  assert.equal(screen.states[0].status, "failed"); assert.equal(screen.states[0].pending_review, null);
  assert.equal(nodes(screen.render(), node => node.type === ReviewCard).length, 0);
});

test("a newly emitted review_required pauses generation without calling confirmation automatically", async () => {
  const calls = [], initial = session("idle"), paused = session();
  const screen = view("features/creation/CreationPanel.tsx", "CreationPanel", panelProps, { states: [initial], modules: { "@/lib/api/sessions": {
    getSession: async () => paused,
    executeStage: async (id, stage, event) => { calls.push(["execute", id, stage]); event({ type: "review_required", session: paused, pending_review: pending }); },
    confirmSessionReview: async () => { calls.push("confirm"); }, updateSessionModels: async () => { calls.push("models"); },
  } } });
  const generate = nodes(screen.render(), node => node.type === Button && text(node) === "生成参考图")[0];
  assert.ok(generate); await generate.props.onClick(); await settle();
  const tree = screen.render();
  assert.deepEqual(calls, [["execute", "story", "reference_generation"]]);
  assert.equal(nodes(tree, node => node.type === ReviewCard)[0].props.busy, false);
  assert.equal(screen.states[0].status, "awaiting_review"); assert.equal(screen.states[1], false);
  assert.equal(nodes(tree, node => node.props?.role === "alert").length, 0);
});

test("failed compliance sessions expose no manual override card and invalid pending IDs are blocked in the panel", async () => {
  const blocked = view("features/creation/CreationPanel.tsx", "CreationPanel", panelProps, { states: [{ ...session("failed"), error: "涉及合规问题，已停止生成。" }] });
  assert.equal(nodes(blocked.render(), node => node.type === ReviewCard).length, 0);
  let requests = 0;
  const invalid = view("features/creation/CreationPanel.tsx", "CreationPanel", panelProps, { states: [{ ...session(), pending_review: { ...pending, review_id: " " } }], modules: { "@/lib/api/sessions": { confirmSessionReview: async () => { requests++; } } } });
  await nodes(invalid.render(), node => node.type === ReviewCard)[0].props.onDecision(true);
  assert.equal(requests, 0);
});
