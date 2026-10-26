import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url), ts = require("typescript");
function compile(file, modules, globals = {}) {
  const exports = {};
  const source = ts.transpileModule(fs.readFileSync(new URL(`../${file}`, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  vm.runInNewContext(source, { exports, Error, Headers, Event, ...globals, require: id => { if (!(id in modules)) throw new Error(`Missing module ${id}`); return modules[id]; } });
  return exports;
}

test("public demo blocks JSON, SSE and raw deletion before fetch; guest reads carry no account credentials", async () => {
  const notices = [], calls = [];
  const demo = compile("lib/demo.ts", {}, { process: { env: { NEXT_PUBLIC_READ_ONLY_DEMO: "1" } }, window: { dispatchEvent: event => notices.push(event.type) } });
  const globals = { fetch: async (url, options) => { calls.push({ url, options }); return Response.json([]); }, localStorage: { getItem: () => "private-existing-account-token" }, window: {} };
  const client = compile("lib/api/client.ts", { "../config": { API_BASE: "/dream" }, "../demo": demo }, globals);
  const stream = compile("lib/stream.ts", { "./api/client": client, "./config": { API_BASE: "/dream" }, "./demo": demo }, globals);
  const sessions = compile("lib/api/sessions.ts", { "./client": client, "../config": { API_BASE: "/dream" }, "../demo": demo, "../stream": stream }, globals);
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) await assert.rejects(client.api("/api/sessions", { method }), /演示版只开放示例作品/);
  await assert.rejects(stream.streamSSE("/api/sessions/example/resume", { method: "POST" }, () => {}), /演示版只开放示例作品/);
  await assert.rejects(sessions.deleteSession("example"), /演示版只开放示例作品/);
  assert.equal(calls.length, 0); assert.equal(notices.length, 6);
  await client.api("/api/sessions");
  assert.equal(calls.length, 1); assert.equal(calls[0].options.headers.has("Authorization"), false);
});

test("ordinary application builds retain their existing write behavior", () => {
  const demo = compile("lib/demo.ts", {}, { process: { env: {} } });
  assert.equal(demo.READ_ONLY_DEMO, false);
  assert.doesNotThrow(() => demo.guardDemoWrite("POST"));
});
