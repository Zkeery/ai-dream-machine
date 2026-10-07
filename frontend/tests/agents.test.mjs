import assert from "node:assert/strict";
import test from "node:test";
import { emptyDraft, readDraft, creationInput } from "../lib/workflow.ts";

test("new drafts opt into agents, persisted legacy drafts keep workflow, choices survive reload", () => {
  for (const type of ["story", "comic"]) {
    const draft = emptyDraft();
    assert.equal(creationInput(draft[type], type).orchestration_mode, "multi_agent");
    draft[type].orchestrationMode = "workflow";
    const loaded = readDraft({ getItem: () => JSON.stringify(draft) }, "account");
    assert.equal(creationInput(loaded[type], type).orchestration_mode, "workflow");
    delete draft[type].orchestrationMode;
    const legacy = readDraft({ getItem: () => JSON.stringify(draft) }, "account");
    assert.equal(legacy[type].orchestrationMode, "workflow");
  }
});
