import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";
import * as workflow from "../lib/workflow.ts";

const require = createRequire(import.meta.url), ts = require("typescript");
const transpile = path => ts.transpileModule(fs.readFileSync(new URL(path, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
const componentSource = transpile("../features/cinema/SessionLibrary.tsx"), apiSource = transpile("../lib/api/sessions.ts");
const story = { session_id: "story", idea: "故事作品", status: "session_completed", current_stage: "post_production", updated_at: 1, video_ratio: "16:9", artifacts: { post_production: { final_video: "/video/story.mp4" } } };
const comic = { session_id: "comic", project_type: "comic", idea: "漫剧作品", status: "idle", updated_at: 2, video_ratio: "9:16", artifacts: {} };

function harness({ confirmed = true, outcome = async () => {}, sessions = [story, comic], tasks = [] } = {}) {
  const states = [], refs = [], deleted = [], selected = [], confirms = [], requests = [];
  let index = 0, refIndex = 0, reloads = 0;
  const Empty = () => null, exports = {};
  const modules = {
    react: { useState: initial => { const i = index++; if (!(i in states)) states[i] = typeof initial === "function" ? initial() : initial; return [states[i], value => { states[i] = typeof value === "function" ? value(states[i]) : value; }]; }, useEffect: () => {}, useRef: initial => { const i = refIndex++; return refs[i] ??= { current: initial }; } },
    "react/jsx-runtime": require("react/jsx-runtime"),
    "lucide-react": { ArrowUpRight: Empty, Clapperboard: Empty, RefreshCw: Empty, Trash2: Empty },
    "@/lib/api/sessions": { deleteSession: async id => { requests.push(id); await outcome(id); } },
    "@/lib/api/tasks": {}, "@/lib/workflow": workflow, "@/lib/media": {}, "@/features/creation/media": { MediaVideo: Empty },
  };
  vm.runInNewContext(componentSource, { exports, Error, window: { confirm: message => { confirms.push(message); return confirmed; } }, require: id => modules[id] });
  const text = node => Array.isArray(node) ? node.map(text).join("") : node && typeof node === "object" ? text(node.props?.children) : node == null || typeof node === "boolean" ? "" : String(node);
  function render() {
    index = 0; refIndex = 0;
    const tree = exports.SessionLibrary({ sessions, tasks, error: "", selectedId: "story", onReload: () => { reloads++; }, onSelect: id => selected.push(id), onDeleted: id => deleted.push(id) });
    const nodes = [];
    function walk(node, ancestors = []) { if (Array.isArray(node)) return node.forEach(child => walk(child, ancestors)); if (!node || typeof node !== "object") return; nodes.push({ node, ancestors, label: text(node) }); walk(node.props?.children, [...ancestors, node]); }
    walk(tree);
    return { nodes, buttons: nodes.filter(item => item.node.type === "button"), text: text(tree) };
  }
  const event = { prevented: 0, stopped: 0, preventDefault() { this.prevented++; }, stopPropagation() { this.stopped++; } };
  return { render, requests, confirms, deleted, selected, states, event, reloads: () => reloads };
}
const removalButton = (view, title) => view.render().buttons.find(item => item.node.props["aria-label"] === `删除${title}`);
const flush = () => new Promise(resolve => setImmediate(resolve));

test("delete actions belong only to story/comic cards and never nest inside the open-card button", () => {
  const view = harness({ tasks: [{ task_id: "shortcut", type: "talking_head", input: { script: "快捷口播" }, status: "completed", updated_at: 3 }] });
  const deletes = view.render().buttons.filter(item => item.node.props.className === "work-delete");
  assert.equal(deletes.length, 2);
  for (const button of deletes) assert.equal(button.ancestors.some(node => node.props?.className === "work-open"), false);
  removalButton(view, "故事作品").node.props.onClick(view.event);
  assert.equal(view.event.prevented, 1); assert.equal(view.event.stopped, 1); assert.deepEqual(view.selected, []);
});

test("canceling the confirmation keeps all projects and makes no deletion request", () => {
  const view = harness({ confirmed: false });
  removalButton(view, "漫剧作品").node.props.onClick(view.event);
  assert.deepEqual(view.requests, []); assert.deepEqual(view.deleted, []); assert.equal(view.reloads(), 0);
  assert.ok(removalButton(view, "漫剧作品"));
  assert.match(view.confirms[0], /知识库不受影响/);
});

test("pending deletion prevents duplicate clicks; success removes the session and preview while preserving a same-ID task", async () => {
  let finish;
  const wait = new Promise(resolve => { finish = resolve; });
  const view = harness({ outcome: () => wait, tasks: [{ task_id: "story", type: "talking_head", input: { script: "独立快捷口播" }, status: "completed", updated_at: 3 }] });
  view.render().buttons.find(item => item.label === "预览成片" && item.ancestors.some(node => node.key === "session:story")).node.props.onClick();
  assert.ok(view.render().nodes.find(item => item.node.props?.className === "library-preview studio-panel"));
  const button = removalButton(view, "故事作品");
  button.node.props.onClick(view.event); button.node.props.onClick(view.event);
  assert.deepEqual(view.requests, ["story"]); assert.equal(view.confirms.length, 1);
  assert.equal(removalButton(view, "故事作品").node.props.disabled, true);
  assert.equal(removalButton(view, "故事作品").label, "删除中…");
  finish(); await flush();
  assert.equal(removalButton(view, "故事作品"), undefined); assert.ok(removalButton(view, "漫剧作品"));
  assert.ok(view.render().buttons.find(item => item.node.props["aria-label"] === "打开独立快捷口播"));
  assert.equal(view.render().nodes.some(item => item.node.props?.className === "library-preview studio-panel"), false);
  assert.deepEqual(view.deleted, ["story"]); assert.equal(view.reloads(), 1); assert.match(view.render().text, /已删除「故事作品」/);
});

test("a server busy rejection leaves the project and active reference intact, with a retryable error", async () => {
  const view = harness({ outcome: async () => { throw new Error("当前作品正在生成，请等待完成后再删除"); } });
  removalButton(view, "故事作品").node.props.onClick(view.event); await flush();
  assert.ok(removalButton(view, "故事作品")); assert.equal(removalButton(view, "故事作品").node.props.disabled, false);
  assert.match(view.render().text, /当前作品正在生成/); assert.deepEqual(view.deleted, []); assert.equal(view.reloads(), 0);
});

function deletionApi(response, calls) {
  class ApiError extends Error { constructor(code, message, status) { super(message); this.code = code; this.status = status; } }
  const exports = {};
  vm.runInNewContext(apiSource, { exports, Error, fetch: async (url, options) => { calls.push({ url, options }); return response; }, require: id => id === "./client" ? { ApiError, authHeaders: () => ({ Authorization: "Bearer test-account" }) } : id === "../config" ? { API_BASE: "" } : {} });
  return exports.deleteSession;
}
test("session DELETE uses same-origin auth and accepts both no-content and JSON success responses", async () => {
  for (const response of [new Response(null, { status: 204 }), Response.json({ deleted: true })]) {
    const calls = []; await deletionApi(response, calls)("session 你好");
    assert.equal(calls[0].url, "/api/sessions/session%20%E4%BD%A0%E5%A5%BD");
    assert.equal(calls[0].options.method, "DELETE"); assert.equal(calls[0].options.headers.Authorization, "Bearer test-account");
  }
});
test("session DELETE preserves server ownership/busy errors without treating them as successful removal", async () => {
  const calls = [], del = deletionApi(Response.json({ error: { code: "SESSION_RUNNING", message: "当前作品正在生成，请等待完成后再删除" } }, { status: 409 }), calls);
  await assert.rejects(del("story"), error => error.code === "SESSION_RUNNING" && error.status === 409 && /正在生成/.test(error.message));
});
