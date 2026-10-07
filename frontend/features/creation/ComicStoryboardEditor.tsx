"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { getComicVoices, type ComicShot, type ComicStoryboardArtifact, type ComicVoice } from "@/lib/api/comic";
import type { CharacterDesignArtifact, ScriptCharacter } from "@/lib/api/sessions";
import { COMIC_MOTIONS, comicStoryboardSave } from "@/lib/comic";

export function ComicStoryboardEditor({ artifact, characters, design, userId, sessionId, versionId, disabled, onSave }: {
  artifact: ComicStoryboardArtifact; characters: ScriptCharacter[]; design?: CharacterDesignArtifact;
  userId: string; sessionId: string; versionId?: string; disabled: boolean;
  onSave: (modifications: Record<string, unknown>) => Promise<boolean>;
}) {
  const key = `dream-machine:comic-edit:${encodeURIComponent(userId)}:${sessionId}:${versionId ?? "current"}`;
  const [value, setValue] = useState<ComicStoryboardArtifact>(() => {
    try { const saved = JSON.parse(localStorage.getItem(key) ?? "null"); return saved && Array.isArray(saved.shots) && saved.voice_map && typeof saved.voice_map === "object" ? saved : artifact; }
    catch { return artifact; }
  });
  const [voices, setVoices] = useState<ComicVoice[]>([]);
  const [voiceError, setVoiceError] = useState("");
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const speakers = [{ id: "narrator", name: "旁白" }, ...characters.map(c => ({ id: c.character_id, name: c.name }))];
  useEffect(() => {
    let active = true;
    getComicVoices().then(data => { if (active) { setVoices(data.voices); setVoiceError(""); } }).catch(e => { if (active) setVoiceError(e instanceof Error ? e.message : "声线列表加载失败"); });
    return () => { active = false; };
  }, []);
  function update(next: ComicStoryboardArtifact) {
    setValue(next); setError("");
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { setError("浏览器无法保存编辑草稿，请保留本页。"); }
  }
  function shot(index: number, fields: Partial<ComicShot>) { update({ ...value, shots: value.shots.map((s, i) => i === index ? { ...s, ...fields } : s) }); }
  function voice(id: string, selected: string) { const next = { ...value.voice_map }; if (selected) next[id] = selected; else delete next[id]; update({ ...value, voice_map: next }); }
  function reference(index: number, kind: "character_ids" | "setting_ids", id: string, checked: boolean) { const current = value.shots[index][kind] ?? []; shot(index, { [kind]: checked ? [...new Set([...current, id])] : current.filter(value => value !== id) }); }
  async function save() {
    try {
      setError("");
      const request = comicStoryboardSave(value, characters.map(c => c.character_id), voices.map(v => v.voice));
      if (await onSave(request)) { try { localStorage.removeItem(key); } catch {} setOpen(false); }
    } catch (e) { setError(e instanceof Error ? e.message : "镜头对白保存失败"); }
  }
  const area = (label: string, text: string, change: (text: string) => void) => <label className="block space-y-1"><span className="text-xs text-muted">{label}</span><textarea className="artifact-field" rows={2} value={text} disabled={disabled} maxLength={4000} onChange={e => change(e.target.value)} /></label>;
  return <section className="artifact-editor comic-editor">
    <Button variant="secondary" disabled={disabled} onClick={() => setOpen(!open)}>{open ? "收起镜头编辑" : "编辑对白 / 声线 / 运镜"}</Button>
    {open && <div className="space-y-4 mt-3">
      <div className="comic-voice-grid">{speakers.map(speaker => <label key={speaker.id} className="block space-y-1"><span className="text-xs text-muted">{speaker.name} · 声线</span><select className="version-select" disabled={disabled || !voices.length} value={value.voice_map[speaker.id] ?? ""} onChange={e => voice(speaker.id, e.target.value)}><option value="">默认声线</option>{voices.map(v => <option key={v.voice} value={v.voice}>{v.name} · {v.gender}</option>)}</select></label>)}</div>
      {voiceError && <p className="text-sm text-danger" role="alert">{voiceError}<button className="text-link ml-2" onClick={() => void getComicVoices().then(data => { setVoices(data.voices); setVoiceError(""); }).catch(e => setVoiceError(e instanceof Error ? e.message : "声线加载失败"))}>重试加载声线</button></p>}
      {value.shots.map((item, index) => <article className="artifact-edit-card" key={item.shot_id}>
        <h4>镜头 {index + 1} · {item.shot_id}</h4>
        {area("画面描述", item.description, description => shot(index, { description }))}
        {area("漫画生成描述", item.prompt, prompt => shot(index, { prompt }))}
        {design && <div className="shot-references">{[...(design.characters ?? []).map(item => ({ item, field: "character_ids" as const })), ...(design.settings ?? []).map(item => ({ item, field: "setting_ids" as const }))].map(({ item: referenceItem, field }) => <label key={`${field}:${referenceItem.id}`}><input type="checkbox" disabled={disabled} checked={(item[field] ?? []).includes(referenceItem.id)} onChange={e => reference(index, field, referenceItem.id, e.target.checked)} />{referenceItem.name}</label>)}</div>}
        <p className="text-xs text-muted">每镜头最多使用 3 张角色 / 场景参考图。</p>
        <div className="comic-shot-options"><label><span>基础运镜</span><select className="version-select" disabled={disabled} value={item.motion} onChange={e => shot(index, { motion: e.target.value as ComicShot["motion"] })}>{Object.entries(COMIC_MOTIONS).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label><label><span>无对白停留时长（秒）</span><input className="version-select" type="number" min={1} max={10} step={0.5} disabled={disabled || Boolean(item.dialogues.length)} value={item.silent_duration} onChange={e => shot(index, { silent_duration: Number(e.target.value) })} /></label></div>
        <div className="comic-dialogues">{item.dialogues.map((line, lineIndex) => <div className="comic-dialogue-edit" key={line.line_id}><label><span className="sr-only">镜头 {index + 1} 第 {lineIndex + 1} 句角色</span><select className="version-select" disabled={disabled} value={line.speaker_id} onChange={e => shot(index, { dialogues: item.dialogues.map((old, i) => i === lineIndex ? { ...old, speaker_id: e.target.value } : old) })}>{speakers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label><label className="comic-line-text"><span className="sr-only">镜头 {index + 1} 第 {lineIndex + 1} 句对白</span><textarea className="artifact-field" rows={2} maxLength={400} disabled={disabled} placeholder="输入对白，最多 400 字" value={line.text} onChange={e => shot(index, { dialogues: item.dialogues.map((old, i) => i === lineIndex ? { ...old, text: e.target.value } : old) })} /></label><label className="comic-line-emotion"><span className="sr-only">情绪备注</span><input className="version-select" disabled={disabled} value={line.emotion ?? ""} maxLength={80} placeholder="情绪备注" onChange={e => shot(index, { dialogues: item.dialogues.map((old, i) => i === lineIndex ? { ...old, emotion: e.target.value } : old) })} /></label><button className="text-link" disabled={disabled} onClick={() => shot(index, { dialogues: item.dialogues.filter((_, i) => i !== lineIndex) })} aria-label={`删除镜头${index + 1}第${lineIndex + 1}句对白`}>移除</button></div>)}</div>
        <button className="text-link" disabled={disabled || item.dialogues.length >= 8} onClick={() => shot(index, { dialogues: [...item.dialogues, { line_id: crypto.randomUUID(), speaker_id: "narrator", text: "", emotion: "" }] })}>＋ 添加对白（{item.dialogues.length} / 8）</button>
      </article>)}
      {error && <p className="text-sm text-danger" role="alert">{error}</p>}
      <Button disabled={disabled || !voices.length} onClick={() => void save()}>保存镜头方案</Button>
    </div>}
  </section>;
}
