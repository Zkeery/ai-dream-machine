"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { COMIC_MOTIONS } from "@/lib/comic";
import type { ComicAudioArtifact, ComicCompositionArtifact, ComicPanelsArtifact, ComicStoryboardArtifact } from "@/lib/api/comic";
import type { ScriptCharacter } from "@/lib/api/sessions";
import { pendingGenerationTargets, targetedGenerationRequest } from "@/lib/workflow";
import { downloadMedia } from "@/lib/media";
import { MediaAudio, MediaImage, MediaVideo } from "./media";

export function ComicStageView({ sessionId, stage, artifact, characters, disabled, generationBlocked, stale, onSelect, onRegenerate }: {
  sessionId: string; stage: string; artifact: unknown; characters: ScriptCharacter[];
  disabled: boolean; generationBlocked: boolean; stale: boolean;
  onSelect: (collection: string, id: string, path: string) => void;
  onRegenerate: (modifications: Record<string, unknown>) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState("");
  const busy = disabled || generationBlocked;
  const name = (id: string) => id === "narrator" ? "旁白" : characters.find(c => c.character_id === id)?.name ?? id;
  function generate(ids: string[], known: string[]) {
    try { setError(""); onRegenerate(targetedGenerationRequest(stage, ids, known)); }
    catch (e) { setError(e instanceof Error ? e.message : "请选择有效的镜头"); }
  }
  function toggle(id: string, checked: boolean) { setSelected(current => checked ? [...new Set([...current, id])] : current.filter(value => value !== id)); }
  function range(ids: string[], affected: string) {
    const pending = pendingGenerationTargets(artifact, ids), targets = selected.filter(id => ids.includes(id));
    return <div className="target-generation-panel space-y-3"><div className="flex gap-3 items-center flex-wrap"><strong>本次已选 {targets.length} / {ids.length} 个镜头</strong><button className="text-link" disabled={busy} onClick={() => setSelected(ids)}>全选</button><button className="text-link" disabled={busy} onClick={() => setSelected([])}>清空选择</button>{pending.length > 0 && <button className="text-link" disabled={busy} onClick={() => setSelected(pending)}>选择待更新项（{pending.length}）</button>}</div><p>{stage === "comic_audio" ? "生成所选镜头的配音，复用对白和声线未变的句子。" : "仅更新所选漫画镜头，保留其他镜头与历史版本。"}受影响下游：{affected}。剩余待更新项仍需完成。</p>{pending.length > 0 && <p className="pending-item-list">仍待更新：{pending.map(id => `镜头 ${ids.indexOf(id) + 1}`).join("、")}</p>}<Button variant="secondary" disabled={busy || !targets.length} onClick={() => generate(targets, ids)}>生成所选 {targets.length} 个镜头{stage === "comic_audio" ? "配音" : "画面"}</Button>{generationBlocked && <p>请先更新并确认前面的阶段。</p>}{error && <p className="text-danger" role="alert">{error}</p>}</div>;
  }
  if (stage === "comic_storyboard") {
    const art = artifact as ComicStoryboardArtifact;
    return <div className="space-y-3">{(art.shots ?? []).map((shot, index) => <article className="comic-shot-card" key={shot.shot_id}><div className="comic-shot-heading"><strong>镜头 {index + 1}</strong><span>{COMIC_MOTIONS[shot.motion]} · {shot.dialogues?.length ? `${shot.dialogues.length} 句对白` : `${shot.silent_duration} 秒无对白`}</span></div><p>{shot.description}</p><small className="text-muted">漫画描述：{shot.prompt}</small><div className="comic-dialogue-list">{(shot.dialogues ?? []).map(line => <p key={line.line_id}><strong>{name(line.speaker_id)}</strong><span>{line.text}</span>{line.emotion && <small>备注：{line.emotion}</small>}</p>)}</div></article>)}</div>;
  }
  if (stage === "comic_panels") {
    const art = artifact as ComicPanelsArtifact, ids = (art.shots ?? []).map(shot => shot.shot_id);
    return <div className="space-y-4">{range(ids, "漫剧合成")}<div className="comic-panel-grid">{(art.shots ?? []).map((shot, index) => <article className="comic-shot-card" key={shot.shot_id}><label className="target-shot-check"><input type="checkbox" disabled={busy} checked={selected.includes(shot.shot_id)} onChange={e => toggle(shot.shot_id, e.target.checked)} />镜头 {index + 1}{art.stale_items?.includes(shot.shot_id) && <span className="pending-item-badge">待更新</span>}</label>{shot.selected || shot.path ? <MediaImage key={shot.selected || shot.path} sessionId={sessionId} path={shot.selected || shot.path} alt={`漫画镜头 ${index + 1}`} /> : <p className="text-muted">尚无镜头图</p>}{shot.versions?.length > 1 && <div className="design-versions">{shot.versions.map((path, i) => <button key={path} disabled={disabled} className={path === (shot.selected || shot.path) ? "active" : ""} aria-pressed={path === (shot.selected || shot.path)} onClick={() => onSelect("shots", shot.shot_id, path)}>版本 {i + 1}{path === (shot.selected || shot.path) ? " · 已选用" : ""}</button>)}</div>}<Button variant="secondary" disabled={busy} onClick={() => generate([shot.shot_id], ids)}>只重新生成镜头 {index + 1}</Button></article>)}</div></div>;
  }
  if (stage === "comic_audio") {
    const art = artifact as ComicAudioArtifact, ids = (art.shots ?? []).map(shot => shot.shot_id);
    return <div className="space-y-4">{range(ids, "漫剧合成")}{(art.shots ?? []).map((shot, index) => <article className="comic-shot-card" key={shot.shot_id}><label className="target-shot-check"><input type="checkbox" disabled={busy} checked={selected.includes(shot.shot_id)} onChange={e => toggle(shot.shot_id, e.target.checked)} />镜头 {index + 1}{art.stale_items?.includes(shot.shot_id) && <span className="pending-item-badge">待更新</span>}</label>{!shot.lines?.length && <p className="text-muted">无对白</p>}{(shot.lines ?? []).map(line => <div className="comic-audio-line" key={line.line_id}><strong>{name(line.speaker_id)}</strong><p>{line.text}</p><small>{line.voice}</small>{line.path ? <MediaAudio key={line.path} sessionId={sessionId} path={line.path} label={`${name(line.speaker_id)}：${line.text}`} /> : <p className="text-danger">此句配音尚未生成。</p>}</div>)}<Button variant="secondary" disabled={busy} onClick={() => generate([shot.shot_id], ids)}>生成镜头 {index + 1} 的配音</Button></article>)}</div>;
  }
  if (stage === "comic_composition") {
    const art = artifact as ComicCompositionArtifact;
    return <div className="space-y-4">{art.final_video ? <div className="comic-final-video"><MediaVideo key={art.final_video} sessionId={sessionId} path={art.final_video} /></div> : <p className="text-muted">漫剧视频尚未生成。</p>}<p className="text-xs text-muted">{Number.isFinite(art.duration) ? `${art.duration.toFixed(1)} 秒 · ` : ""}{art.width} × {art.height} · {art.fps} fps</p>{art.final_video && !stale && <Button onClick={() => void downloadMedia(sessionId, "漫剧视频.mp4").catch(e => setError(e instanceof Error ? e.message : "漫剧下载失败"))}>下载漫剧 MP4</Button>}{error && <p className="text-danger" role="alert">{error}</p>}</div>;
  }
  return <p className="text-muted">该漫剧阶段暂无产物。</p>;
}
