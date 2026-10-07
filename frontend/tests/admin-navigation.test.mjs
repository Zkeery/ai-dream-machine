import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";
import * as workflow from "../lib/workflow.ts";

const require = createRequire(import.meta.url), ts = require("typescript"), jsx = require("react/jsx-runtime");
const compile = file => ts.transpileModule(fs.readFileSync(new URL(file, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
const workspaceSource = compile("../features/workspace/Workspace.tsx");
const sandboxSource = compile("../features/sandbox/SandboxPanel.tsx"), adminSource = compile("../lib/api/admin.ts");
const flattenText = node => Array.isArray(node) ? node.map(flattenText).join("") : node && typeof node === "object" ? flattenText(node.props?.children) : node == null || typeof node === "boolean" ? "" : String(node);

function workspace(isAdmin, query = "") {
  const user = { user_id: "current-account", ...(isAdmin === undefined ? {} : { is_admin: isAdmin }) };
  const states = [], navigations = [], exports = {}, screens = [];
  let stateIndex = 0, params = new URLSearchParams(query), draft = workflow.emptyDraft();
  const router = { push: url => { navigations.push(url); params = new URL(url, "https://studio.test").searchParams; }, replace: url => navigations.push(url) };
  const screen = name => () => { screens.push(name); return jsx.jsx("section", { "data-screen": name }); };
  const Empty = () => null;
  const modules = {
    react: { useState: initial => { const i = stateIndex++; if (!(i in states)) states[i] = typeof initial === "function" ? initial() : initial; return [states[i], update => { states[i] = typeof update === "function" ? update(states[i]) : update; }]; }, useEffect: () => {}, useCallback: callback => callback },
    "react/jsx-runtime": jsx,
    "next/navigation": { useRouter: () => router, useSearchParams: () => params },
    "lucide-react": { Clapperboard: Empty, FlaskConical: Empty, LogOut: Empty, Plus: Empty, Settings: Empty, Zap: Empty },
    "@/features/auth/AuthProvider": { useAuth: () => ({ user, loading: false, logout: async () => {} }) },
    "@/lib/workflow": workflow,
    "@/lib/useWorkspaceDraft": { useWorkspaceDraft: () => ({ draft, updateDraft: update => { draft = update(draft); }, storageError: "" }) },
    "@/lib/api/sessions": { listSessions: async () => [] }, "@/lib/api/tasks": { listTasks: async () => [] },
    "@/features/creation/CreationPanel": { CreationPanel: screen("CreationPanel") },
    "@/features/pipelines/PipelinesPanel": { PipelinesPanel: screen("PipelinesPanel") },
    "@/features/sandbox/SandboxPanel": { SandboxPanel: screen("SandboxPanel") },
    "@/features/settings/SettingsPanel": { SettingsPanel: screen("SettingsPanel") },
    "@/features/cinema/CinemaHome": { CinemaHome: screen("CinemaHome") },
    "@/features/cinema/SessionLibrary": { SessionLibrary: screen("SessionLibrary"), sessionTitle: workflow.titleOfSession },
    "@/features/knowledge/KnowledgePanel": { KnowledgePanel: screen("KnowledgePanel") },
    "./UsageSummary": { UsageSummary: screen("UsageSummary") },
  };
  vm.runInNewContext(workspaceSource, { exports, Error, URLSearchParams, window: { scrollTo: () => {} }, require: id => modules[id] });
  function render() {
    stateIndex = 0; screens.length = 0;
    const nodes = [];
    function visit(node, parents = []) {
      if (Array.isArray(node)) return node.forEach(child => visit(child, parents));
      if (!node || typeof node !== "object") return;
      if (typeof node.type === "function") return visit(node.type(node.props), parents);
      nodes.push({ node, parents, label: flattenText(node) }); visit(node.props?.children, [...parents, node]);
    }
    visit(exports.Workspace());
    return { nodes, buttons: nodes.filter(item => item.node.type === "button"), screens: [...screens] };
  }
  return { render, navigations };
}

test("false, absent or invalid admin claims cannot mount settings or sandbox, even through direct routes", () => {
  for (const claim of [false, undefined, "true", 1]) for (const view of ["settings", "sandbox"]) {
    const app = workspace(claim, `view=${view}&is_admin=true`), result = app.render();
    assert.equal(result.screens.includes("SettingsPanel"), false); assert.equal(result.screens.includes("SandboxPanel"), false);
    assert.equal(result.buttons.some(item => ["设置", "沙盒"].includes(item.node.props["aria-label"])), false);
    assert.ok(result.nodes.some(item => item.node.props?.role === "alert" && /仅供管理员使用/.test(item.label)));
    const back = result.buttons.find(item => item.label === "返回创作台"); assert.ok(back); back.node.props.onClick();
    assert.equal(app.navigations.at(-1), "/"); assert.ok(app.render().screens.includes("CinemaHome"));
    assert.equal(result.nodes.some(item => item.node.type === "footer" && item.node.props?.children?.some?.(child => child?.type === "button")), false);
  }
});

test("a true server admin claim exposes utility navigation and mounts the requested administrative panel", () => {
  for (const [label, view, panel] of [["设置", "settings", "SettingsPanel"], ["沙盒", "sandbox", "SandboxPanel"]]) {
    const app = workspace(true), button = app.render().buttons.find(item => item.node.props["aria-label"] === label);
    assert.ok(button); button.node.props.onClick(); assert.equal(app.navigations.at(-1), `/?view=${view}`);
    assert.ok(app.render().screens.includes(panel)); assert.ok(workspace(true, `view=${view}`).render().screens.includes(panel));
  }
});

test("ordinary accounts keep all four business navigation paths and their actual target components", () => {
  for (const claim of [false, undefined]) for (const [label, url, panel] of [["创作台", "/", "CinemaHome"], ["我的作品", "/?view=projects", "SessionLibrary"], ["短视频工具", "/?view=pipelines&tool=literary", "PipelinesPanel"], ["知识库", "/?view=knowledge", "KnowledgePanel"]]) {
    const app = workspace(claim), button = app.render().buttons.find(item => item.label === label && item.parents.some(parent => parent.type === "nav"));
    assert.ok(button); button.node.props.onClick(); assert.equal(app.navigations.at(-1), url); assert.ok(app.render().screens.includes(panel));
  }
});

function sandbox(response) {
  const states = [], effects = [], calls = [], apiExports = {}, exports = {};
  let stateIndex = 0, effectRegistered = false;
  vm.runInNewContext(adminSource, { exports: apiExports, require: () => ({ api: async path => { calls.push(path); if (response instanceof Error) throw response; return response; } }) });
  const modules = { "react/jsx-runtime": jsx, "@/lib/api/admin": apiExports, react: { useState: initial => { const i = stateIndex++; if (!(i in states)) states[i] = initial; return [states[i], value => { states[i] = value; }]; }, useEffect: effect => { if (!effectRegistered) { effectRegistered = true; effects.push(effect); } } } };
  vm.runInNewContext(sandboxSource, { exports, Error, require: id => modules[id] });
  const render = () => { stateIndex = 0; return exports.SandboxPanel(); };
  render(); effects.forEach(effect => effect());
  return { render, calls };
}

test("the real sandbox loads status and counts solely from the protected admin endpoint", async () => {
  const app = sandbox({ status: "ok", session_count: 7, task_count: 3 });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(app.calls, ["/api/admin/sandbox"]);
  const text = flattenText(app.render()); assert.match(text, /正常/); assert.match(text, /我的作品7/); assert.match(text, /短管线任务3/);
});

test("a rejected admin sandbox request shows the server error instead of a healthy state or fabricated counts", async () => {
  const app = sandbox(new Error("仅管理员可访问此功能")); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(app.calls, ["/api/admin/sandbox"]); const text = flattenText(app.render());
  assert.match(text, /仅管理员可访问此功能/); assert.match(text, /无法读取/); assert.doesNotMatch(text, /正常/);
});
