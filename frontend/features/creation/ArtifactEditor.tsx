"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import type { CharacterDesignArtifact } from "@/lib/api/sessions";

export function ArtifactEditor({ artifact, userId, sessionId, stage, versionId, disabled, onSave, design }: {
  artifact: unknown; userId: string; sessionId: string; stage: string; versionId?: string; disabled: boolean;
  onSave: (artifact: Record<string, unknown>, regenerate: boolean) => Promise<boolean>;
  design?: CharacterDesignArtifact;
}) {
  const storageKey = `dream-machine:edit:${encodeURIComponent(userId)}:${sessionId}:${stage}:${versionId ?? "legacy"}`;
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(() => {
    try { return localStorage.getItem(storageKey) || JSON.stringify(artifact, null, 2); }
    catch { return JSON.stringify(artifact, null, 2); }
  });
  const [error, setError] = useState("");
  let value: Record<string, unknown>;
  try { const parsed: unknown = JSON.parse(text); value = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : artifact as Record<string, unknown>; } catch { value = artifact as Record<string, unknown>; }
  const list = (key: string): Record<string, unknown>[] => Array.isArray(value[key]) ? value[key] as Record<string, unknown>[] : [];
  function update(text: string) {
    setText(text);
    try { localStorage.setItem(storageKey, text); } catch { setError("编辑草稿无法写入浏览器存储，请保留当前页面。"); }
  }
  function field(key: string, item: unknown) { update(JSON.stringify({ ...value, [key]: item }, null, 2)); }
  function itemField(key: string, index: number, fieldName: string, item: unknown) { field(key, list(key).map((old, i) => i === index ? { ...old, [fieldName]: item } : old)); }
  function references(index: number, kind: "character_ids" | "setting_ids", id: string, checked: boolean) {
    const shot = list("shots")[index];
    const current = Array.isArray(shot[kind]) ? shot[kind] as string[] : (kind === "character_ids" ? design?.characters : design?.settings)?.map(item => item.id) ?? [];
    itemField("shots", index, kind, checked ? [...new Set([...current, id])] : current.filter(value => value !== id));
  }
  const area = (label: string, content: unknown, change: (value: string) => void) => <label className="block space-y-1"><span className="text-xs text-muted">{label}</span><textarea value={String(content ?? "")} disabled={disabled} onChange={e => change(e.target.value)} rows={3} className="artifact-field" /></label>;
  async function save(regenerate: boolean) {
    let value: unknown;
    try { value = JSON.parse(text); if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(); }
    catch { setError("内容需为有效的 JSON 对象。请检查引号、逗号和字段结构。"); return; }
    setError("");
    if (await onSave(value as Record<string, unknown>, regenerate)) {
      try { localStorage.removeItem(storageKey); } catch {}
      setOpen(false);
    }
  }
  return <div className="artifact-editor">
    <Button variant="secondary" disabled={disabled} onClick={() => setOpen(!open)}>{open ? "收起编辑" : "编辑内容"}</Button>
    {open && <div className="space-y-4 mt-3">
      {stage === "script_generation" ? <>
        <label className="block space-y-1"><span className="text-xs text-muted">作品名称</span><Input value={String(value.title ?? "")} disabled={disabled} onChange={e => field("title", e.target.value)} /></label>
        {area("故事梗概", value.logline, text => field("logline", text))}
        {area("整体情绪", value.mood, text => field("mood", text))}
        {list("characters").map((item, index) => <div className="artifact-edit-card" key={String(item.character_id ?? index)}><h4>角色 · {String(item.name ?? index + 1)}</h4>{area("角色描述", item.description, text => itemField("characters", index, "description", text))}</div>)}
        {list("settings").map((item, index) => <div className="artifact-edit-card" key={String(item.setting_id ?? index)}><h4>场景 · {String(item.name ?? index + 1)}</h4>{area("场景描述", item.description, text => itemField("settings", index, "description", text))}</div>)}
        {list("episodes").map((item, index) => <div className="artifact-edit-card" key={index}><h4>第 {String(item.episode_number ?? index + 1)} 集</h4><Input aria-label={`第${index + 1}集标题`} value={String(item.act_title ?? "")} disabled={disabled} onChange={e => itemField("episodes", index, "act_title", e.target.value)} />{area("剧集内容", item.content, text => itemField("episodes", index, "content", text))}</div>)}
      </> : <>

        {list("shots").map((shot, index) => <div className="artifact-edit-card" key={String(shot.shot_id ?? index)}><h4>镜头 {index + 1}</h4>{area("画面描述", shot.description, text => itemField("shots", index, "description", text))}{area("生成描述", shot.prompt, text => itemField("shots", index, "prompt", text))}{design && <div className="shot-references">{[...(design.characters ?? []).map(item => ({ item, field: "character_ids" as const })), ...(design.settings ?? []).map(item => ({ item, field: "setting_ids" as const }))].map(({ item, field }) => <label key={`${field}:${item.id}`}><input type="checkbox" disabled={disabled} checked={Array.isArray(shot[field]) ? (shot[field] as string[]).includes(item.id) : true} onChange={e => references(index, field, item.id, e.target.checked)} />{item.name}</label>)}</div>}</div>)}
      </>}
      <details className="text-xs text-muted"><summary>高级：查看完整内容</summary><textarea aria-label="编辑阶段内容" className="artifact-json mt-2" value={text} onChange={e => update(e.target.value)} disabled={disabled} rows={18} spellCheck={false} /></details>
      {error && <p className="text-sm text-danger" role="alert">{error}</p>}<div className="flex gap-3 flex-wrap"><Button disabled={disabled} onClick={() => void save(false)}>保存修改</Button><Button variant="secondary" disabled={disabled} onClick={() => void save(true)}>按修改重新生成</Button></div>
    </div>}
  </div>;
}
