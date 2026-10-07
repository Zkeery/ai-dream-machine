import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";
import * as workflow from "../lib/workflow.ts";

const require = createRequire(import.meta.url), ts = require("typescript");
const Empty = () => null, Button = () => null, Recovery = () => null;
const settle = () => new Promise(resolve => setImmediate(resolve));
function text(node) { return Array.isArray(node) ? node.map(text).join("") : node && typeof node === "object" ? text(node.props?.children) : node == null ? "" : String(node); }
function nodes(tree, predicate) { const result = []; function walk(node) { if (Array.isArray(node)) return node.forEach(walk); if (!node || typeof node !== "object") return; if (predicate(node)) result.push(node); walk(node.props?.children); } walk(tree); return result; }
function compile(file, modules, globals = {}) {
  const source = ts.transpileModule(fs.readFileSync(new URL(`../${file}`, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
  const exports = {};
  vm.runInNewContext(source, { exports, Error, AbortController, ...globals, require: id => modules[id] ?? { CreationForm: Empty, ScriptView: Empty, StageView: Empty, ArtifactEditor: Empty, ComicStageView: Empty, ComicStoryboardEditor: Empty, KnowledgeSources: Empty, SessionKnowledge: Empty, KnowledgeLibraryPicker: Empty, AssetPicker: Empty } });
  return exports;
}
function view(file, name, props, { states = [], modules = {}, runEffects = true } = {}) {
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
  const exports = compile(file, { react, "react/jsx-runtime": require("react/jsx-runtime"), "@/components/ui/Button": { Button }, "@/components/ui/Input": { Input: Empty }, "@/lib/workflow": workflow, "@/features/cinema/SessionLibrary": { STAGE_CN: {}, TaskVideo: Empty }, "./ModelSelector": { ModelSelector: Empty, ModelUsageNote: Empty }, "@/features/creation/ModelSelector": { ModelSelector: Empty, ModelUsageNote: Empty }, "./RecoveryNotice": { RecoveryNotice: Recovery }, "@/features/creation/RecoveryNotice": { RecoveryNotice: Recovery }, "@/lib/useModelCatalog": { useModelCatalog: () => ({ catalog: null, loading: false, error: "模型目录离线", retry: () => {} }) }, ...modules }, { setTimeout: () => 1, clearTimeout: () => {}, localStorage: { getItem: () => null } });
  return { states, render: () => { stateIndex = refIndex = effectIndex = 0; const tree = exports[name](props); for (const effect of effects) { if (effect.callback) { effect.cleanup = effect.callback(); delete effect.callback; } } return tree; }, unmount: () => effects.forEach(effect => effect.cleanup?.()) };
}
const recoverable = { execution_id: "latest-execution", source_execution_id: "original-execution", can_resume: true, requires_reconciliation: false, jobs: [{ kind: "video", model: "wan2.7-i2v", status: "submitted", resumable: true }] };
const recoveryApi = compile("lib/api/recovery.ts", { "./client": {}, "../stream": {} });
const usageApi = compile("lib/api/usage.ts", { "./client": {} });

test("recovery and usage APIs use authenticated routes, frozen execution IDs and the distinct resume endpoints", async () => {
  const calls = [], streams = [];
  const client = { api: async (path, options) => { calls.push({ path, options }); return { task_id: "task" }; } };
  const api = compile("lib/api/recovery.ts", { "./client": client, "../stream": { streamSSE: async (path, options) => { streams.push({ path, options }); } } });
  await api.getRecovery("session", "owned/id"); await api.getRecovery("task", "task");
  await api.resumeTask("task", "source-1"); await api.resumeSession("owned/id", "source-2", () => {});
  const usage = compile("lib/api/usage.ts", { "./client": client }); await usage.getAccountUsage();
  assert.deepEqual(calls.map(item => item.path), ["/api/sessions/owned%2Fid/recovery", "/api/tasks/task/recovery", "/api/tasks/task/resume", "/api/usage"]);
  assert.deepEqual(JSON.parse(calls[2].options.body), { execution_id: "source-1" });
  assert.equal(streams[0].path, "/api/sessions/owned%2Fid/resume"); assert.deepEqual(JSON.parse(streams[0].options.body), { execution_id: "source-2" });
  for (const message of ["本月生成额度不足", "当前并发任务已满", "模型价格未配置"]) {
    const rejected = compile("lib/api/recovery.ts", { "./client": { api: async () => { throw new Error(message); } } });
    await assert.rejects(rejected.resumeTask("task", "source"), error => error.message === message);
  }
});

test("a resumable failed stage exposes one continuation action and prevents duplicate clicks", async () => {
  const calls = []; let finish;
  const props = { kind: "session", entityId: "story", status: "failed", busy: false, onResume: id => { calls.push(id); return new Promise(resolve => { finish = resolve; }); } };
  const screen = view("features/creation/RecoveryNotice.tsx", "RecoveryNotice", props, { modules: { "@/lib/api/recovery": { ...recoveryApi, getRecovery: async () => recoverable } } });
  screen.render(); await settle(); const tree = screen.render();
  assert.match(text(tree), /已提交的模型任务只查询进度或下载结果/); assert.match(text(tree), /尚未提交的部分可能正常计费/);
  const button = nodes(tree, node => node.type === Button)[0]; assert.equal(text(button), "继续未完成阶段");
  button.props.onClick(); button.props.onClick(); assert.deepEqual(calls, ["original-execution"]);
  assert.equal(nodes(screen.render(), node => node.type === Button)[0].props.disabled, true);
  finish(); await settle(); screen.unmount();
});

test("unknown model submissions never expose continuation, even if a conflicting can_resume flag is true", async () => {
  for (const status of [{ ...recoverable, requires_reconciliation: true }, { ...recoverable, jobs: [{ kind: "image", model: "qwen-image-2.0", status: "unknown", resumable: false }] }]) {
    let requests = 0;
    const screen = view("features/creation/RecoveryNotice.tsx", "RecoveryNotice", { kind: "task", entityId: "task", status: "interrupted", busy: false, onResume: async () => { requests++; } }, { modules: { "@/lib/api/recovery": { ...recoveryApi, getRecovery: async () => status } } });
    screen.render(); await settle(); const tree = screen.render();
    assert.match(text(tree), /模型侧结果待核对/); assert.equal(nodes(tree, node => node.type === Button).length, 0); assert.equal(requests, 0);
    screen.unmount();
  }
});

test("recovery lookup failure remains retryable and running projects do not offer a new recovery request", async () => {
  let queries = 0;
  const modules = { "@/lib/api/recovery": { ...recoveryApi, getRecovery: async () => { queries++; throw new Error("恢复记录读取失败，请稍后重试"); } } };
  const screen = view("features/creation/RecoveryNotice.tsx", "RecoveryNotice", { kind: "session", entityId: "story", status: "failed", busy: false, onResume: async () => {} }, { modules });
  screen.render(); await settle(); let tree = screen.render();
  assert.match(text(tree), /恢复记录读取失败/); assert.equal(nodes(tree, node => node.type === Button).length, 0);
  nodes(tree, node => node.type === "button")[0].props.onClick(); screen.render(); await settle(); assert.equal(queries, 2); screen.unmount();
  const running = view("features/creation/RecoveryNotice.tsx", "RecoveryNotice", { kind: "session", entityId: "story", status: "running", busy: true, onResume: async () => {} }, { modules });
  assert.equal(running.render(), null); assert.equal(queries, 2); running.unmount();
});

test("changed original inputs show the server reason instead of offering continuation", async () => {
  const status = { ...recoverable, can_resume: false, reason: "上游选用版本已变化，请重新生成", reason_code: "RESUME_INPUT_CHANGED" };
  const screen = view("features/creation/RecoveryNotice.tsx", "RecoveryNotice", { kind: "session", entityId: "story", status: "failed", busy: false, onResume: async () => {} }, { modules: { "@/lib/api/recovery": { ...recoveryApi, getRecovery: async () => status } } });
  screen.render(); await settle(); const tree = screen.render();
  assert.match(text(tree), /上游选用版本已变化，请重新生成/); assert.equal(nodes(tree, node => node.type === Button).length, 0); screen.unmount();
});

test("the real stage continuation does not change chosen models or call ordinary generation", async () => {
  const session = { session_id: "story", status: "interrupted", current_stage: "reference_generation", project_type: "story", artifacts: {}, stages_completed: ["script_generation", "character_design", "storyboard"], stale_stages: [], model_selection: { image: "new-preference" }, video_generation_mode: "first_frame", updated_at: 1, execution: { execution_id: "latest", status: "interrupted" } };
  const calls = [];
  const screen = view("features/creation/CreationPanel.tsx", "CreationPanel", { sessionId: "story", userId: "user", onCreated: () => {}, onChanged: () => {} }, { states: [session], runEffects: false, modules: {
    "@/lib/api/sessions": { getSession: async () => session, updateSessionModels: async () => { calls.push("models"); }, executeStage: async () => { calls.push("execute"); }, interveneSession: async () => { calls.push("intervene"); } },
    "@/lib/api/recovery": { resumeSession: async (id, executionId, event) => { calls.push(["resume", id, executionId]); event({ type: "error", error: { message: "本月生成额度不足" } }); } },
  } });
  await nodes(screen.render(), node => node.type === Recovery)[0].props.onResume("original");
  assert.deepEqual(calls, [["resume", "story", "original"]]); assert.match(text(screen.render()), /本月生成额度不足/);
  assert.equal(screen.states[0].model_selection.image, "new-preference");
});

test("quick-task continuation resumes the same task, then subscribes to its stream without creating another task", async () => {
  const task = { task_id: "task", type: "literary_video", status: "interrupted", input: { text: "原输入" }, result: null, updated_at: 1, execution: { status: "interrupted", execution_id: "latest" } }, calls = [];
  const screen = view("features/pipelines/PipelinesPanel.tsx", "PipelinesPanel", { tab: "literary", taskId: "task", userId: "user", draft: workflow.emptyDraft().tools.literary, onDraftChange: () => { calls.push("draft"); }, onTabChange: () => {}, onOpenTask: () => {}, onCreated: () => {} }, { states: [[], task], runEffects: false, modules: {
    "@/lib/api/recovery": { resumeTask: async (id, executionId) => { calls.push(["resume", id, executionId]); return { task_id: id }; } },
    "@/lib/api/tasks": { getTask: async () => task, listTasks: async () => [task], streamTask: async id => { calls.push(["stream", id]); }, createTask: async () => { calls.push("create"); } },
  } });
  await nodes(screen.render(), node => node.type === Recovery)[0].props.onResume("original");
  assert.deepEqual(calls, [["resume", "task", "original"], ["stream", "task"]]);
});

test("usage shows account balances without exposing provider accounting details", async () => {
  const usage = { scope: "account", month: "2026-10", currency: "CNY", limit_cny: 500, reserved_cny: 12, calculated_cny: 8.5, provider_reported_cny: null, remaining_cny: 479.5, uncertain_calls: 1, estimated_completed_calls: 2, concurrency: { active: 1, account_limit: 2, global_limit: 4 }, missing_price_models: ["unpriced-model"], notice: "按价格表计算，不是供应商实付账单。" };
  const screen = view("features/workspace/UsageSummary.tsx", "UsageSummary", {}, { modules: { "@/lib/api/usage": { ...usageApi, getAccountUsage: async () => usage } } });
  screen.render(); await settle(); const shown = text(screen.render());
  for (const value of ["按表估算¥8.50", "已预留¥12.00", "可用额度¥479.50", "运行中 1 / 2"]) assert.ok(shown.includes(value), value);
  for (const value of ["供应商已报告费用", "unpriced-model", "平台并发上限"]) assert.equal(shown.includes(value), false);
  assert.equal(shown.includes("供应商已报告费用：¥0.00"), false); screen.unmount();
  assert.equal(usageApi.cnyLabel(null), "待核实"); assert.equal(usageApi.cnyLabel(0), "¥0.00");
});

test("a usage error provides a read-only retry without a generation or account-blocking action", async () => {
  let calls = 0;
  const screen = view("features/workspace/UsageSummary.tsx", "UsageSummary", {}, { modules: { "@/lib/api/usage": { ...usageApi, getAccountUsage: async () => { calls++; throw new Error("额度暂时不可用"); } } } });
  screen.render(); await settle(); const tree = screen.render();
  assert.match(text(tree), /额度暂时不可用/); const buttons = nodes(tree, node => node.type === "button"); assert.equal(buttons.length, 1); assert.equal(text(buttons[0]), "重试");
  buttons[0].props.onClick(); screen.render(); await settle(); assert.equal(calls, 2); screen.unmount();
});
