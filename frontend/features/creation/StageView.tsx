"use client";

import { Button } from "@/components/ui/Button";
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

export function StageView({
  sessionId,
  stage,
  artifact,
}: {
  sessionId: string;
  stage: string;
  artifact: unknown;
}) {
  if (stage === "character_design") {
    const art = artifact as CharacterDesignArtifact;
    return (
      <div className="space-y-6">
        {[...(art.characters || []), ...(art.settings || [])].map((item, i) => {
          const paths = (item.versions || []).filter(Boolean);
          const selected = item.selected || paths[0];
          return (
            <div key={item.id || i}>
              <div className="text-sm font-medium text-foreground">
                {item.name}
                {item.description && <span className="text-muted font-normal">：{item.description}</span>}
              </div>
              {selected ? (
                <div className="mt-2 max-w-sm">
                  <MediaImage sessionId={sessionId} path={selected} alt={item.name || "角色/场景图"} />
                </div>
              ) : (
                <p className="text-xs text-muted mt-1">暂无图片</p>
              )}
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
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {(art.shots || []).map((s, i) =>
          s.path ? (
            <div key={s.shot_id || i}>
              <MediaImage sessionId={sessionId} path={s.path} alt={`参考图 ${i + 1}`} />
            </div>
          ) : null,
        )}
      </div>
    );
  }

  if (stage === "video_generation") {
    const art = artifact as VideoArtifact;
    return (
      <div className="space-y-4">
        {(art.segments || []).map((s, i) =>
          s.path ? (
            <div key={s.segment_id || i}>
              <div className="text-xs text-muted mb-1">片段 {i + 1}</div>
              <MediaVideo sessionId={sessionId} path={s.path} />
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
        {art.final_video && (
          <Button onClick={() => void downloadMedia(sessionId)}>下载成片</Button>
        )}
      </div>
    );
  }

  return <p className="text-sm text-muted">该阶段暂无产物展示</p>;
}

export function stageHasArtifact(stage: string, s: SessionMeta): boolean {
  return Boolean(s.artifacts?.[stage]);
}
