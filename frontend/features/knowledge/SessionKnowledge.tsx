"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { KnowledgeLibraryPicker } from "./KnowledgeLibraryPicker";

export function SessionKnowledge({ libraryIds, disabled, onSave }: { libraryIds: string[]; disabled: boolean; onSave: (ids: string[]) => Promise<boolean> }) {
  const [chosen, setChosen] = useState(libraryIds);
  const [saving, setSaving] = useState(false);
  const dirty = chosen.length !== libraryIds.length || chosen.some(id => !libraryIds.includes(id));
  async function save() { setSaving(true); try { await onSave(chosen); } finally { setSaving(false); } }
  return <div className="session-knowledge"><KnowledgeLibraryPicker selected={chosen} onChange={setChosen} disabled={disabled || saving} />{dirty && <div className="knowledge-binding-actions"><Button variant="secondary" disabled={disabled || saving} onClick={() => void save()}>{saving ? "保存中…" : "保存知识库绑定"}</Button><button className="text-link" disabled={disabled || saving} onClick={() => setChosen(libraryIds)}>取消修改</button></div>}</div>;
}
