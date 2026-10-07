import assert from "node:assert/strict";
import test from "node:test";
import { toggleKnowledgeLibrary, unavailableKnowledgeIds, validateKnowledgeFile, knowledgeUploadForm } from "../lib/knowledge.ts";

test("library binding requires explicit choices, limits three, and preserves selection when a fourth is rejected", () => {
  const chosen = ["world", "character", "brand"];
  assert.throws(() => toggleKnowledgeLibrary(chosen, "style", true), /最多选择 3/);
  assert.deepEqual(chosen, ["world", "character", "brand"]);
  assert.deepEqual(toggleKnowledgeLibrary(chosen, "character", false), ["world", "brand"]);
  assert.deepEqual(toggleKnowledgeLibrary(chosen, "world", true), chosen);
});

test("deleted or inaccessible selected libraries stay explicit rather than silently rebinding", () => {
  assert.deepEqual(unavailableKnowledgeIds(["own", "removed"], [{ library_id: "own" }, { library_id: "new" }]), ["removed"]);
  assert.deepEqual(unavailableKnowledgeIds([], [{ library_id: "own" }]), []);
});

test("document client validation rejects empty, oversized and unsupported files before upload", () => {
  for (const name of ["world.md", "roles.TXT", "brand.PDF"]) assert.equal(validateKnowledgeFile({ name, size: 5 * 1024 * 1024 }), "");
  assert.match(validateKnowledgeFile({ name: "empty.md", size: 0 }), /文件为空/);
  assert.match(validateKnowledgeFile({ name: "too-big.txt", size: 5 * 1024 * 1024 + 1 }), /5 MB/);
  assert.match(validateKnowledgeFile({ name: "script.docx", size: 50 }), /支持 Markdown/);
});

test("document multipart omits a blank optional title and preserves category and constraint semantics", () => {
  const file = new File(["世界观正文"], "world.md", { type: "text/markdown" });
  const untitled = knowledgeUploadForm({ file, title: " ", category: "world", is_constraint: false });
  assert.equal(untitled.has("title"), false); assert.equal(untitled.get("file").name, "world.md");
  assert.equal(untitled.get("category"), "world"); assert.equal(untitled.get("is_constraint"), "false");
  const titled = knowledgeUploadForm({ file, title: " 核心规则 ", category: "brand", is_constraint: true });
  assert.equal(titled.get("title"), "核心规则"); assert.equal(titled.get("is_constraint"), "true");
});
