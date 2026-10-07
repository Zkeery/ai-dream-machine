"use client";

import { Button } from "@/components/ui/Button";
import { useState } from "react";
import { downloadMedia } from "@/lib/media";
import type {
  CharacterDesignArtifact,
  PostProductionArtifact,
  ReferenceArtifact,
  SessionMeta,
  StoryboardArtifact,
  VideoArtifact,
} from "@/lib/api/sessions";
import { MediaImage, MediaVideo } from "./media";
import { DesignPromptEditor } from "./DesignPromptEditor";
import { pendingGenerationTargets, targetedGenerationRequest, type ProjectType } from "@/lib/workflow";

export function StageView({
  sessionId,
  stage,
  artifact,
  stale = false,
  disabled = false,
  onSelect,
  onRegenerate,
  userId,
  versionId,
  generationBlocked = false,
  projectType = "story",
}: {
  sessionId: string;
  stage: string;
  artifact: unknown;
  stale?: boolean;
  disabled?: boolean;
  onSelect?: (collection: string, id: string, path: string) => void;
  onRegenerate?: (modifications: Record<string, unknown>) => void;
  userId: string;
  versionId?: string;
  generationBlocked?: boolean;
  projectType?: ProjectType;
}) {
  const [error, setError] = useState("");
  const [selectedTargets, setSelectedTargets] = useState<string[]>([]);
  const generationDisabled = disabled || generationBlocked;
  function regenerate(ids: string[], knownIds: string[], description?: string) {
    try { setError(""); onRegenerate?.(targetedGenerationRequest(stage, ids, knownIds, description)); }
    catch (e) { setError(e instanceof Error ? e.message : "请选择有效生成范围"); }
  }
  function toggle(id: string, checked: boolean) { setSelectedTargets(current => checked ? [...new Set([...current, id])] : current.filter(value => value !== id)); }
  function rangeControls(ids: string[], affected: string) {
    const count = selectedTargets.filter(id => ids.includes(id)).length;
    const pending = pendingGenerationTargets(artifact, ids);
    return <div className="target-generation-panel space-y-3"><div className="flex items-center gap-3 flex-wrap"><strong>本次已选 {count} / {ids.length} 个镜头</strong><button className="text-link" disabled={generationDisabled} onClick={() => setSelectedTargets(ids)}>全选</button><button className="text-link" disabled={generationDisabled} onClick={() => setSelectedTargets([])}>清空选择</button>{pending.length > 0 && <button className="text-link" disabled={generationDisabled} onClick={() => setSelectedTargets(pending)}>选择待更新项（{pending.length}）</button>}</div>{pending.length > 0 && <p className="pending-item-list">仍待更新：{pending.map(id => `镜头 ${ids.indexOf(id) + 1}`).join("、")}。请选择这些镜头重新生成。</p>}<p>生成后保留未选镜头和所有历史版本。受影响下游：{affected}。上游已变时，未更新镜头仍会保留待更新状态。</p><Button variant="secondary" disabled={generationDisabled || count === 0} onClick={() => regenerate(selectedTargets.filter(id => ids.includes(id)), ids)}>重新生成所选 {count} 个镜头</Button>{generationBlocked && <p>请先更新前面的阶段，再生成这里的镜头。</p>}{error && <p className="text-danger" role="alert">{error}</p>}</div>;
  }
  if (stage === "character_design") {
    const art = artifact as CharacterDesignArtifact;
    return (
      <div className="space-y-6">
        {error && <p className="text-sm text-danger" role="alert">{error}</p>}
        {[...(art.characters || []).map(item => ({ ...item, collection: "characters" })), ...(art.settings || []).map(item => ({ ...item, collection: "settings" }))].map((item, i) => {
          const paths = (item.versions || []).filter(Boolean);
          const selected = item.selected || paths[0];
          return (
            <div key={item.id || i}>
              <div className="text-sm font-medium text-foreground">
                {item.name}
                {art.stale_items?.includes(item.id) && <span className="pending-item-badge">待更新</span>}
                {item.description && <span className="text-muted font-normal">：{item.description}</span>}
              </div>
              {selected ? (
                <div className="mt-2 max-w-sm">
                  <MediaImage key={selected} sessionId={sessionId} path={selected} alt={item.name || "角色/场景图"} />
                </div>
              ) : (
                <p className="text-xs text-muted mt-1">暂无图片</p>
              )}
              {paths.length > 1 && <div className="design-versions" aria-label={`${item.name}图片版本`}>{paths.map((path, index) => <button key={path} disabled={disabled} className={selected === path ? "active" : ""} aria-pressed={selected === path} onClick={() => onSelect?.(item.collection, item.id, path)}>版本 {index + 1}{selected === path ? " · 已选用" : " · 选用"}</button>)}</div>}
              <DesignPromptEditor key={`${versionId ?? "legacy"}:${item.id}`} userId={userId} sessionId={sessionId} versionId={versionId} collection={item.collection} itemId={item.id} name={item.name} description={item.description} disabled={generationDisabled} affectedStages={projectType === "comic" ? "镜头对白、漫画镜头和漫剧成片" : undefined} onGenerate={description => regenerate([item.id], [...(art.characters ?? []), ...(art.settings ?? [])].map(item => item.id), description)} />
            </div>
          );
        })}
      </div>
    );
  }

  if (stage === "storyboard") {
    const art = artifact as StoryboardArtifact;
    return (
      <ol className="space-y-3">
        {(art.shots || []).map((s, i) => (
          <li key={s.shot_id || i} className="border border-border rounded-lg p-3">
            <div className="text-sm font-medium text-foreground">镜头 {i + 1}</div>
            {s.description && <p className="text-sm text-muted mt-1">{s.description}</p>}
            {s.prompt && <p className="text-xs text-muted mt-1">提示词：{s.prompt}</p>}
          </li>
        ))}
      </ol>
    );
  }

  if (stage === "reference_generation") {
    const art = artifact as ReferenceArtifact;
    const ids = (art.shots ?? []).map(shot => shot.shot_id);
    return (
      <div className="space-y-4">
        {rangeControls(ids, "视频、成片")}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {(art.shots || []).map((s, i) =>
          s.path ? (
            <div key={s.shot_id || i}>
              <label className="target-shot-check"><input type="checkbox" disabled={generationDisabled} checked={selectedTargets.includes(s.shot_id)} onChange={e => toggle(s.shot_id, e.target.checked)} />镜头 {i + 1}{art.stale_items?.includes(s.shot_id) && <span className="pending-item-badge">待更新</span>}</label>
              <MediaImage key={s.selected || s.path} sessionId={sessionId} path={s.selected || s.path} alt={`参考图 ${i + 1}`} />
              {Boolean(s.versions && s.versions.length > 1) && <div className="design-versions">{s.versions?.map((path, index) => <button key={path} disabled={disabled} className={(s.selected || s.path) === path ? "active" : ""} aria-pressed={(s.selected || s.path) === path} onClick={() => onSelect?.("shots", s.shot_id, path)}>参考版本 {index + 1}{(s.selected || s.path) === path ? " · 已选用" : ""}</button>)}</div>}
              <div className="mt-3 space-y-2"><p className="text-xs text-muted">单独生成 1 个镜头；视频、成片会按实际依赖标记待更新。</p><Button variant="secondary" disabled={generationDisabled} onClick={() => regenerate([s.shot_id], ids)}>只重新生成镜头 {i + 1}</Button></div>
            </div>
          ) : null,
        )}
        </div>
      </div>
    );
  }

  if (stage === "video_generation") {
    const art = artifact as VideoArtifact;
    const ids = (art.segments ?? []).map(segment => segment.shot_id);
    if (!ids.length) return <p className="text-sm text-muted">视频片段尚未生成，请使用上方已关联的参考图开始生成。</p>;
    return (
      <div className="space-y-4">
        {rangeControls(ids, "成片")}
        {(art.segments || []).map((s, i) =>
          s.path ? (
            <div key={s.segment_id || i}>
              <label className="target-shot-check"><input type="checkbox" disabled={generationDisabled} checked={selectedTargets.includes(s.shot_id)} onChange={e => toggle(s.shot_id, e.target.checked)} />片段 {i + 1} · 镜头 {s.shot_id}{art.stale_items?.includes(s.shot_id) && <span className="pending-item-badge">待更新</span>}</label>
              <MediaVideo key={s.selected || s.path} sessionId={sessionId} path={s.selected || s.path} />
              {Boolean(s.versions && s.versions.length > 1) && <div className="design-versions">{s.versions?.map((path, index) => <button key={path} disabled={disabled} className={(s.selected || s.path) === path ? "active" : ""} aria-pressed={(s.selected || s.path) === path} onClick={() => onSelect?.("segments", s.segment_id, path)}>片段版本 {index + 1}{(s.selected || s.path) === path ? " · 已选用" : ""}</button>)}</div>}
              <div className="mt-3 space-y-2"><p className="text-xs text-muted">单独生成 1 个镜头片段；成片会按实际依赖标记待更新。</p><Button variant="secondary" disabled={generationDisabled} onClick={() => regenerate([s.shot_id], ids)}>只重新生成片段 {i + 1}</Button></div>
            </div>
          ) : null,
        )}
      </div>
    );
  }

  if (stage === "post_production") {
    const art = artifact as PostProductionArtifact;
    return (
      <div className="space-y-4">
        {art.final_video ? (
          <MediaVideo sessionId={sessionId} path={art.final_video} />
        ) : (
          <p className="text-sm text-muted">成片尚未生成</p>
        )}
        {art.final_video && !stale && (
          <Button onClick={() => void downloadMedia(sessionId).catch(e => setError(e instanceof Error ? e.message : "下载失败"))}>下载成片</Button>
        )}
        {error && <p className="text-sm text-danger" role="alert">{error}</p>}
      </div>
    );
  }

  return <p className="text-sm text-muted">该阶段暂无产物展示</p>;
}

export function stageHasArtifact(stage: string, s: SessionMeta): boolean {
  return Boolean(s.artifacts?.[stage]);
}
