"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { CreationForm } from "./CreationForm";
import { ScriptView } from "./ScriptView";
import { StageView } from "./StageView";
import {
  continueSession,
  executeStage,
  getSession,
  type ScriptArtifact,
  type SessionMeta,
} from "@/lib/api/sessions";

const STAGE_CN: Record<string, string> = {
  script_generation: "剧本",
  character_design: "角色场景",
  storyboard: "分镜",
  reference_generation: "参考图",
  video_generation: "视频",
  post_production: "成片",
};

const STAGE_ORDER = [
  "script_generation",
  "character_design",
  "storyboard",
  "reference_generation",
  "video_generation",
  "post_production",
];

function scriptOf(s: SessionMeta): ScriptArtifact | null {
  return (s.artifacts?.script_generation as ScriptArtifact | undefined) ?? null;
}

export function CreationPanel({
  sessionId,
  onCreated,
}: {
  sessionId: string | null;
  onCreated: (s: SessionMeta) => void;
}) {
  const [session, setSession] = useState<SessionMeta | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState({ message: "", percent: 0 });
  const [error, setError] = useState("");

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    getSession(sessionId)
      .then((s) => {
        if (!cancelled) setSession(s);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "加载失败");
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  const generate = useCallback(async () => {
    if (!sessionId || !session) return;
    const stage = session.current_stage;
    if (!stage) return;
    setRunning(true);
    setError("");
    setProgress({ message: `正在生成${STAGE_CN[stage] ?? stage}…`, percent: 0 });
    try {
      await executeStage(sessionId, stage, (ev) => {
        if (ev.type === "progress") {
          setProgress({ message: ev.message, percent: ev.percent });
        } else if (ev.type === "done") {
          setSession(ev.session as unknown as SessionMeta);
        } else if (ev.type === "error") {
          setError(ev.error.message);
        }
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "生成失败");
    } finally {
      setRunning(false);
    }
  }, [sessionId, session]);

  const next = useCallback(async () => {
    if (!sessionId) return;
    try {
      setSession(await continueSession(sessionId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    }
  }, [sessionId]);

  if (!sessionId) {
    return <CreationForm onCreated={onCreated} />;
  }

  if (!session) {
    return <p className="text-muted">加载作品…</p>;
  }

  const stage = session.current_stage;
  const stageName = stage ? (STAGE_CN[stage] ?? stage) : "已完成";
  const script = scriptOf(session);
  const isLast = stage === STAGE_ORDER[STAGE_ORDER.length - 1];
  const hasArtifact = stage ? Boolean(session.artifacts?.[stage]) : false;
  const completed = session.status === "stage_completed";
  const doneAll = session.status === "session_completed";

  return (
    <div className="space-y-5 max-w-3xl">
      {/* 阶段进度步骤条 */}
      <div className="flex items-center gap-1 flex-wrap">
        {STAGE_ORDER.map((st, i) => {
          const done = session.stages_completed.includes(st);
          const cur = session.current_stage === st;
          return (
            <div key={st} className="flex items-center gap-1">
              {i > 0 && <span className="w-4 h-px bg-border" />}
              <span
                className={`px-2 py-1 rounded-full text-xs ${
                  done
                    ? "bg-success/15 text-success"
                    : cur
                      ? "bg-primary text-white"
                      : "bg-surface-2 text-muted"
                }`}
              >
                {STAGE_CN[st] ?? st}
              </span>
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-3">
        <h2 className="text-base font-semibold text-foreground">{stageName}阶段</h2>
        <span className="text-xs text-muted">状态：{session.status}</span>
      </div>

      {running && (
        <div className="space-y-2">
          <div className="h-1.5 rounded-full bg-surface-2 overflow-hidden">
            <div
              className="h-full bg-primary transition-all"
              style={{ width: `${progress.percent}%` }}
            />
          </div>
          <p className="text-sm text-muted">
            {progress.message}（{progress.percent}%）
          </p>
        </div>
      )}

      {error && <p className="text-sm text-danger">{error}</p>}

      {stage === "script_generation" && script ? (
        <ScriptView script={script} />
      ) : hasArtifact && stage && stage !== "script_generation" ? (
        <StageView sessionId={session.session_id} stage={stage} artifact={session.artifacts[stage]} />
      ) : (
        !running && (
          <div className="space-y-3">
            <p className="text-sm text-muted">
              {doneAll ? "全部完成。" : `点「生成${stageName}」开始。`}
            </p>
          </div>
        )
      )}

      {!running && !doneAll && (
        <div className="flex gap-3">
          <Button onClick={() => void generate()}>
            {hasArtifact ? `重新生成${stageName}` : `生成${stageName}`}
          </Button>
          {completed && !isLast && (
            <Button variant="secondary" onClick={() => void next()}>
              确认进入下一阶段
            </Button>
          )}
        </div>
      )}

      {doneAll && <p className="text-sm text-success">🎉 全部完成，可在上方下载成片。</p>}
    </div>
  );
}
