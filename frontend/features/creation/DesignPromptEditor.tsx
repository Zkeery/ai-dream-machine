"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";

export function DesignPromptEditor({ userId, sessionId, versionId, collection, itemId, name, description, disabled, onGenerate, affectedStages = "分镜、参考图、视频和成片" }: {
  userId: string; sessionId: string; versionId?: string; collection: string; itemId: string; name: string; description: string; disabled: boolean;
  onGenerate: (description: string) => void;
  affectedStages?: string;
}) {
  const storageKey = `dream-machine:design-prompt:${encodeURIComponent(userId)}:${sessionId}:${versionId ?? "legacy"}:${collection}:${itemId}`;
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState(() => {
    try { return localStorage.getItem(storageKey) ?? description; } catch { return description; }
  });
  const [error, setError] = useState("");
  function update(value: string) {
    setPrompt(value);
    try { localStorage.setItem(storageKey, value); } catch { setError("提示词草稿无法保存，请保留当前页面。"); }
  }
  return <div className="design-prompt-editor mt-3">
    <Button variant="secondary" disabled={disabled} onClick={() => setOpen(!open)}>{open ? "收起提示词" : "修改提示词 / 重新生成"}</Button>
    {open && <div className="space-y-3 mt-3"><label className="block"><span className="text-xs text-muted">{name}的画面描述</span><textarea className="artifact-field mt-2" value={prompt} disabled={disabled} maxLength={4000} rows={4} onChange={e => update(e.target.value)} aria-label={`${name}提示词`} /></label><p className="text-xs text-muted">本次生成 1 个{collection === "characters" ? "角色" : "场景"}。旧图片保留；{affectedStages}将按实际依赖标记待更新。</p>{error && <p className="text-sm text-danger" role="alert">{error}</p>}<Button disabled={disabled || !prompt.trim() || prompt.trim().length > 4000} onClick={() => onGenerate(prompt.trim())}>重新生成这张图</Button></div>}
  </div>;
}
