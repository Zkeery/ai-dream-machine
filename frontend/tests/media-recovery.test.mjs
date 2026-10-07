import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = ts.transpileModule(fs.readFileSync(new URL("../features/creation/media.tsx", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText;

function harness(component) {
  const states = [], effects = [], requests = [], revoked = [];
  let stateIndex = 0, effectIndex = 0;
  const exports = {};
  const react = {
    useState(initial) {
      const index = stateIndex++;
      if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial;
      return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }];
    },
    useEffect(callback, dependencies) {
      const index = effectIndex++, previous = effects[index];
      if (!previous || dependencies.some((value, i) => value !== previous.dependencies[i])) {
        previous?.cleanup?.();
        effects[index] = { dependencies, callback };
      }
    },
  };
  vm.runInNewContext(source, {
    exports,
    URL: { revokeObjectURL: url => revoked.push(url) },
    require: id => id === "react" ? react : id === "react/jsx-runtime" ? require(id) : {
      loadMediaBlob: (sessionId, path) => new Promise((resolve, reject) => requests.push({ sessionId, path, resolve, reject })),
    },
  });
  function render(path, sessionId = "test-session") {
    stateIndex = effectIndex = 0;
    const tree = exports[component]({ sessionId, path, alt: "测试图片", label: "测试配音" });
    for (const effect of effects) {
      if (effect.callback) { effect.cleanup = effect.callback(); delete effect.callback; }
    }
    return tree;
  }
  return { render, requests, revoked, unmount: () => effects.forEach(effect => effect.cleanup?.()) };
}

const settle = () => new Promise(resolve => setImmediate(resolve));
const variants = [["MediaImage", "img", "图片加载失败"], ["MediaVideo", "video", "视频加载失败"], ["MediaAudio", "audio", "配音加载失败"]];

for (const [component, tag, errorText] of variants) {
  test(`${component} clears a previous failure when the path changes and still reports a new failure`, async () => {
    const view = harness(component);
    view.render("old"); view.requests[0].reject(new Error("temporary failure")); await settle();
    assert.match(view.render("old").props.children, new RegExp(errorText));
    const loading = view.render("new");
    assert.doesNotMatch(loading.props.children, new RegExp(errorText));
    view.requests[1].resolve("blob:new"); await settle();
    const loaded = view.render("new");
    assert.equal(loaded.type, tag); assert.equal(loaded.props.src, "blob:new");
    view.render("broken"); view.requests[2].reject(new Error("new failure")); await settle();
    assert.match(view.render("broken").props.children, new RegExp(errorText));
    assert.deepEqual(view.revoked, ["blob:new"]);
    view.unmount();
  });
}

test("a delayed old response cannot replace a newer video and its object URL is released", async () => {
  const view = harness("MediaVideo");
  view.render("old"); view.render("new");
  view.requests[1].resolve("blob:new"); await settle();
  view.requests[0].resolve("blob:old"); await settle();
  assert.equal(view.render("new").props.src, "blob:new");
  assert.deepEqual(view.revoked, ["blob:old"]);
  view.unmount();
  assert.deepEqual(view.revoked, ["blob:old", "blob:new"]);
});

test("a delayed old error cannot replace a successfully loaded new video", async () => {
  const view = harness("MediaVideo");
  view.render("same-path", "old-session"); view.render("same-path", "new-session");
  view.requests[1].resolve("blob:new-session"); await settle();
  view.requests[0].reject(new Error("late old error")); await settle();
  assert.equal(view.render("same-path", "new-session").props.src, "blob:new-session");
  view.unmount();
});

test("an unmounted media request releases a late response without retaining it", async () => {
  const view = harness("MediaAudio");
  view.render("pending"); view.unmount();
  view.requests[0].resolve("blob:late"); await settle();
  assert.deepEqual(view.revoked, ["blob:late"]);
});
