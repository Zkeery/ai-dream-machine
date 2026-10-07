import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";
import * as workflow from "../lib/workflow.ts";

const require = createRequire(import.meta.url);
const ts = require("typescript");
// Exercise the actual TSX component and its confirmation callback without a DOM or network.
const source = ts.transpileModule(fs.readFileSync(new URL("../features/creation/CreationPanel.tsx", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;

function fixture(type = "comic", overrides = {}) {
  const stage = type === "comic" ? "comic_composition" : "post_production";
  return { session_id: "owned-session", project_type: type, status: "stage_completed", current_stage: stage, artifacts: { [stage]: { final_video: "/video/final.mp4" } }, stages_completed: workflow.PROJECT_STAGES[type], stale_stages: [], ...overrides };
}

function harness(session, response = { ...session, status: "session_completed" }) {
  const states = [session, false, null, "", { message: "", percent: 0 }, "", ""];
  const calls = [];
  let index = 0, changes = 0;
  const Button = () => null, Empty = () => null;
  const exports = {};
  const modules = {
    react: { useState: () => { const current = index++; return [states[current], value => { states[current] = typeof value === "function" ? value(states[current]) : value; }]; }, useEffect: () => {}, useCallback: fn => fn, useRef: value => ({ current: value }) },
    "react/jsx-runtime": require("react/jsx-runtime"),
    "@/lib/workflow": workflow,
    "@/lib/useModelCatalog": { useModelCatalog: () => ({ catalog: null, loading: false, error: "", retry: () => {} }) },
    "./ModelSelector": { ModelSelector: Empty, ModelUsageNote: Empty },
    "@/components/ui/Button": { Button },
    "@/features/cinema/SessionLibrary": { STAGE_CN: {} },
    "@/lib/api/sessions": { continueSession: async id => { calls.push(id); if (response instanceof Error) throw response; return response; } },
    "@/lib/media": {},
  };
  vm.runInNewContext(source, { exports, Error, require: id => modules[id] ?? { CreationForm: Empty, ScriptView: Empty, StageView: Empty, ArtifactEditor: Empty, SessionKnowledge: Empty, KnowledgeSources: Empty, ComicStageView: Empty, ComicStoryboardEditor: Empty } });
  const text = value => Array.isArray(value) ? value.map(text).join("") : value && typeof value === "object" ? text(value.props?.children) : value == null ? "" : String(value);
  function render() {
    index = 0;
    const tree = exports.CreationPanel({ sessionId: session.session_id, userId: "a", onCreated: () => {}, onChanged: () => { changes++; } });
    const buttons = [];
    function visit(node) { if (Array.isArray(node)) return node.forEach(visit); if (!node || typeof node !== "object") return; if (node.type === Button) buttons.push({ label: text(node.props.children), ...node.props }); visit(node.props?.children); }
    visit(tree);
    return { buttons, text: text(tree) };
  }
  return { render, states, calls, changes: () => changes };
}

test("comic and legacy story films archive through server continue and then remove the confirmation action", async () => {
  for (const type of ["comic", "story"]) {
    const session = fixture(type); if (type === "story") delete session.project_type;
    const confirmed = { ...session, status: "session_completed" };
    const view = harness(session, confirmed);
    const button = view.render().buttons.find(button => button.label === "确认完成并归档");
    assert.ok(button);
    button.onClick();
    // During the request the action disappears; only the server response completes the session.
    assert.equal(view.states[0].status, "stage_completed");
    assert.equal(view.render().buttons.some(button => button.label === "确认完成并归档"), false);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(view.calls, [session.session_id]); assert.equal(view.states[0], confirmed); assert.equal(view.changes(), 1);
    assert.equal(view.render().buttons.some(button => button.label === "确认完成并归档"), false);
  }
});

test("stale, running, missing video or already archived films cannot be confirmed", () => {
  for (const overrides of [{ stale_stages: ["comic_composition"] }, { stale_stages: ["comic_audio"] }, { status: "running" }, { execution: { status: "pending" } }, { artifacts: {} }, { status: "session_completed" }, { stages_completed: ["script_generation"] }]) {
    const view = harness(fixture("comic", overrides));
    assert.equal(view.render().buttons.some(button => button.label === "确认完成并归档"), false); assert.deepEqual(view.calls, []);
  }
});

test("a rejected archive preserves server stage state, shows the error and offers an explicit retry", async () => {
  const session = fixture(), view = harness(session, new Error("当前产物已失效，请先重新生成"));
  view.render().buttons.find(button => button.label === "确认完成并归档").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(view.states[0], session); assert.equal(view.changes(), 0);
  assert.match(view.render().text, /当前产物已失效/);
  assert.ok(view.render().buttons.find(button => button.label === "确认完成并归档"));
});
