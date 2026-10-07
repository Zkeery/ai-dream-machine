"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { CreationForm } from "./CreationForm";
import { ScriptView } from "./ScriptView";
import { StageView } from "./StageView";
import { ArtifactEditor } from "./ArtifactEditor";
import { STAGE_CN } from "@/features/cinema/SessionLibrary";
import { finalVideoOf, modelSelectionError, videoFormatError, normalizeModelSelection, resolvedModelSelection, stageModelKeys, PROJECT_LABELS, projectStageState, STATUS_LABELS, stageGenerationRequest } from "@/lib/workflow";
import { continueSession, executeStage, getSession, interveneSession, updateSessionKnowledge, updateSessionModels, type CharacterDesignArtifact, type ReferenceArtifact, type VideoArtifact, type ScriptArtifact, type SessionMeta } from "@/lib/api/sessions";
import type { SSEEvent } from "@/lib/stream";
import { downloadMedia } from "@/lib/media";
import { SessionKnowledge } from "@/features/knowledge/SessionKnowledge";
import { ComicStageView } from "./ComicStageView";
import { ComicStoryboardEditor } from "./ComicStoryboardEditor";
import type { ComicStoryboardArtifact } from "@/lib/api/comic";
import type { ModelSelection } from "@/lib/api/models";
import { useModelCatalog } from "@/lib/useModelCatalog";
import { ModelSelector } from "./ModelSelector";
import { RecoveryNotice } from "./RecoveryNotice";
import { resumeSession } from "@/lib/api/recovery";
import { MediaImage } from "./media";

export function CreationPanel({ sessionId, userId, onCreated, onChanged }: { sessionId: string | null; userId: string; onCreated: (s: SessionMeta) => void; onChanged: () => void }) {
  const [session, setSession] = useState<SessionMeta | null>(null);
  const [requestPending, setRequestPending] = useState(false);
  const [viewStage, setViewStage] = useState<string | null>(null);
  const [videoMode, setVideoMode] = useState<string>(() => {
    try { return localStorage.getItem(`dream-machine:video-mode:${userId}:${sessionId}`) || ""; } catch { return ""; }
  });
  const [progress, setProgress] = useState({ message: "", percent: 0 });
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [modelSaving, setModelSaving] = useState(false);
  const models = useModelCatalog();
  const mounted = useRef(true);
  const streamController = useRef<AbortController | null>(null);
  const requestInFlight = useRef(false);
  const sessionRevision = useRef(0);
  const [clock, setClock] = useState(Date.now);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    const revision = sessionRevision.current;
    try {
      const value = await getSession(sessionId);
      if (mounted.current && revision === sessionRevision.current) { setSession(value); setLoadError(""); }
    } catch (e) { if (mounted.current) setLoadError(e instanceof Error ? e.message : "作品加载失败"); }
  }, [sessionId]);

  useEffect(() => {
    mounted.current = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { await refresh(); if (mounted.current) timer = setTimeout(poll, 1800); };
    void poll();
    return () => { mounted.current = false; clearTimeout(timer); streamController.current?.abort(); };
  }, [refresh]);

  const event = (ev: SSEEvent) => {
    if (!mounted.current) return;
    if (ev.type === "progress") setProgress({ message: ev.message, percent: ev.percent });
    else if (ev.type === "done") { sessionRevision.current++; setSession(ev.session as unknown as SessionMeta); onChanged(); }
    else if (ev.type === "error") setError(ev.error.message);
  };

  const running = requestPending || session?.status === "running" || session?.execution?.status === "pending" || session?.execution?.status === "running";
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const { projectType, stageOrder, finalStage, stage, stale, blocked, canConfirm } = projectStageState(session, viewStage ?? undefined);
  const stageName = STAGE_CN[stage] ?? stage;
  const artifact = session?.artifacts[stage];
  const referenceShots = ((session?.artifacts.reference_generation as ReferenceArtifact | undefined)?.shots ?? []).filter(shot => shot.selected || shot.path);
  const hasVideoOutput = ((session?.artifacts.video_generation as VideoArtifact | undefined)?.segments ?? []).some(segment => segment.selected || segment.path);
  const hasCurrentOutput = stage === "video_generation" ? hasVideoOutput : stage === "reference_generation" ? referenceShots.length > 0 : Boolean(artifact);
  const referencesReady = Boolean(session?.stages_completed.includes("reference_generation") && !session.stale_stages?.includes("reference_generation") && referenceShots.length);
  const canOpenVideo = referencesReady && !blocked && (canConfirm || Boolean(session && stageOrder.indexOf(session.current_stage ?? finalStage) > stageOrder.indexOf("reference_generation")));
  const modelKeys = stageModelKeys(stage, videoMode || session?.video_generation_mode || "first_frame");
  if (session?.orchestration_mode === "multi_agent" && !modelKeys.includes("text")) modelKeys.push("text");
  const selection = resolvedModelSelection(session?.model_selection, models.catalog, modelKeys);
  const modelError = modelSelectionError(selection, models.catalog, modelKeys);
  const formatError = stage === "video_generation" && session ? videoFormatError(selection, models.catalog, modelKeys, session.video_ratio, session.resolution) : "";
  const modelsReady = !modelKeys.length || (!models.loading && !models.error && !modelError && !formatError);
  const generationBlocked = blocked || !modelsReady || Boolean(formatError) || (stage === "video_generation" && !referencesReady);
  const canArchive = Boolean(session && !running && stage === finalStage && stage === session.current_stage && session.status === "stage_completed" && !blocked && !session.stale_stages?.length && finalVideoOf(session));
  const versions = (session?.artifact_versions?.[stage] ?? []).filter(
    version => version.reason === "generated" || version.reason === "legacy",
  );
  const currentVersion = versions.some(version => version.version_id === session?.selected_versions?.[stage])
    ? session?.selected_versions?.[stage]
    : undefined;

  async function persistModels(selection: ModelSelection): Promise<void> {
    if (!sessionId || !session) throw new Error("作品尚未加载");
    setModelSaving(true);
    try {
      const next = await updateSessionModels(sessionId, { ...normalizeModelSelection(session.model_selection), ...selection });
      if (Object.entries(selection).some(([key, id]) => next.model_selection?.[key as keyof ModelSelection] !== id)) throw new Error("服务端未保存所选模型，请重新选择。尚未提交生成。");
      sessionRevision.current++;
      if (mounted.current) setSession(next);
      onChanged();
    } finally { if (mounted.current) setModelSaving(false); }
  }

  async function prepareModels(): Promise<void> {
    if (!modelsReady) throw new Error(models.error || modelError || "模型目录加载中，请稍候。");
    if (modelKeys.some(key => session?.model_selection?.[key] !== selection[key])) await persistModels(selection);
  }

  async function saveModels(value: ModelSelection) {
    if (!sessionId || !session || running || requestInFlight.current) return;
    const invalid = modelSelectionError(value, models.catalog, modelKeys);
    if (invalid) { setError(invalid); return; }
    requestInFlight.current = true;
    setRequestPending(true); setError("");
    try { await persistModels(value); }
    catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : "模型选择保存失败，尚未提交生成。"); }
    finally { requestInFlight.current = false; if (mounted.current) setRequestPending(false); }
  }

  async function generate() {
    if (!sessionId || !session || running || requestInFlight.current || generationBlocked) return;
    const chosenMode = videoMode || session.video_generation_mode;
    const request = stageGenerationRequest(stage, artifact, session.video_generation_mode, chosenMode);
    if (request.kind === "intervene") {
      await modify(request.modifications);
      return;
    }
    const waitingHint = stage === "script_generation" || stage === "storyboard" || stage === "comic_storyboard"
      ? `模型正在生成${stageName}，约需 1～3 分钟，请稍候`
      : `正在生成${stageName}…`;
    setRequestPending(true); setError(""); setProgress({ message: waitingHint, percent: 5 });
    requestInFlight.current = true;
    streamController.current = new AbortController();
    try { await prepareModels(); await executeStage(sessionId, stage, event, streamController.current.signal); }
    catch (e) { if (mounted.current && !(e instanceof Error && e.name === "AbortError")) setError(e instanceof Error ? e.message : "连接中断，正在查询后台状态。请勿重复提交。"); }
    finally { requestInFlight.current = false; if (mounted.current) { await refresh(); setRequestPending(false); onChanged(); } }
  }

  async function modify(modifications: Record<string, unknown>): Promise<boolean> {
    if (!sessionId || running || requestInFlight.current) return false;
    if (modifications.operation !== "save" && modifications.operation !== "select" && generationBlocked) return false;
    requestInFlight.current = true;
    setRequestPending(true); setError("");
    let success = true;
    try { if (modifications.operation !== "save" && modifications.operation !== "select") await prepareModels(); await interveneSession(sessionId, stage, modifications, ev => { if (ev.type === "error") success = false; event(ev); }); }
    catch (e) { success = false; if (mounted.current) setError(e instanceof Error ? e.message : "修改失败"); }
    finally { requestInFlight.current = false; if (mounted.current) { await refresh(); setRequestPending(false); onChanged(); } }
    return success;
  }

  async function resume(executionId: string): Promise<void> {
    if (!sessionId || !session || running || requestInFlight.current) return;
    requestInFlight.current = true;
    setRequestPending(true); setError(""); setViewStage(null);
    setProgress({ message: "正在继续原来的生成任务…", percent: 0 });
    streamController.current = new AbortController();
    try { await resumeSession(sessionId, executionId, event, streamController.current.signal); }
    catch (e) { if (mounted.current && !(e instanceof Error && e.name === "AbortError")) setError(e instanceof Error ? e.message : "继续失败，正在查询后台状态。"); }
    finally { requestInFlight.current = false; if (mounted.current) { await refresh(); setRequestPending(false); onChanged(); } }
  }

  async function next() {
    if (!sessionId || running || requestInFlight.current || (!canConfirm && !canArchive)) return;
    requestInFlight.current = true;
    setRequestPending(true); setError("");
    try { const nextSession = await continueSession(sessionId); sessionRevision.current++; setSession(nextSession); setViewStage(null); onChanged(); }
    catch (e) { setError(e instanceof Error ? e.message : "操作失败"); }
    finally { requestInFlight.current = false; setRequestPending(false); }
  }

  async function saveKnowledge(ids: string[]): Promise<boolean> {
    if (!sessionId || running) return false;
    setRequestPending(true); setError("");
    try { const next = await updateSessionKnowledge(sessionId, ids); setSession(next); onChanged(); return true; }
    catch (e) { setError(e instanceof Error ? e.message : "知识库绑定保存失败"); return false; }
    finally { setRequestPending(false); }
  }

  if (!sessionId) return <CreationForm onCreated={onCreated} />;
  if (!session) return <div><p className={loadError ? "text-danger" : "text-muted"} role={loadError ? "alert" : "status"}>{loadError || "正在恢复作品与生成状态…"}</p>{loadError && <Button variant="secondary" onClick={() => void refresh()}>重新加载</Button>}</div>;

  const lastEvent = session.execution?.last_event;
  const message = lastEvent?.message || progress.message || "后台正在生成。离开页面后，可从作品库回来继续查看。";
  const percent = lastEvent?.percent ?? progress.percent;
  const stepWaitSeconds = session.execution?.updated_at ? Math.max(0, Math.floor((clock / 1000) - session.execution.updated_at)) : 0;
  const waitingVideo = running && session.execution?.stage === "video_generation";
  const savedSegments = ((session.artifacts.video_generation as VideoArtifact | undefined)?.segments ?? []).filter(segment => segment.selected || segment.path).length;
  const characters = (session.artifacts.script_generation as ScriptArtifact | undefined)?.characters ?? [];

  return <div className="space-y-5 max-w-3xl">
    <p className="project-type-label">{PROJECT_LABELS[projectType]}{projectType === "comic" ? " · 单集漫剧视频" : ""}</p>
    <div className="flex items-center gap-1 flex-wrap" aria-label={`${PROJECT_LABELS[projectType]}阶段`}>{stageOrder.map((st, i) => {
      const done = session.stages_completed.includes(st), active = stage === st, expired = session.stale_stages?.includes(st);
      return <div key={st} className="flex items-center gap-1">{i > 0 && <span className="w-4 h-px bg-border" />}<button onClick={() => setViewStage(st)} aria-pressed={active} className={`px-2 py-1 rounded-full text-xs ${active ? "bg-primary text-primary-ink" : expired ? "bg-danger/15 text-danger" : done ? "bg-success/15 text-success" : "bg-surface-2 text-muted"}`}>{STAGE_CN[st]}{expired ? " · 待更新" : ""}</button></div>;
    })}</div>
    <div className="flex items-center gap-3"><h2 className="text-base font-semibold text-foreground">{stageName}阶段</h2><span className="text-xs text-muted">{STATUS_LABELS[session.status] ?? session.status}</span></div>
    {projectType === "story" && stage === "reference_generation" && <section className="rounded-lg border border-border bg-surface-2 p-4 space-y-3" aria-label="参考图到视频生成">
      <h3 className="text-sm font-semibold">参考图 → 视频片段 → 成片</h3>
      <p className="text-sm text-muted">{referencesReady ? `已准备 ${referenceShots.length} 张参考图` : "请先完成参考图"}</p>
      <Button disabled={running || !canOpenVideo} onClick={() => canConfirm ? void next() : setViewStage("video_generation")}>{canConfirm ? "确认参考图，进入视频生成" : "前往视频生成"}</Button>
      {!canOpenVideo && !running && <p className="text-xs text-muted">请先完成前面的阶段，并生成所有待更新的参考图。</p>}
    </section>}
    {projectType === "story" && stage === "video_generation" && <section className="rounded-lg border border-border bg-surface-2 p-4 space-y-3" aria-label="视频使用的参考图">
      <div className="flex items-center justify-between gap-3 flex-wrap"><h3 className="text-sm font-semibold">用参考图生成视频片段</h3><Button variant="secondary" disabled={running} onClick={() => setViewStage("reference_generation")}>查看或调整参考图</Button></div>
      <p className="text-sm text-muted">{referencesReady ? `已关联 ${referenceShots.length} 张参考图` : "请先完成参考图"}</p>
      {referenceShots.length > 0 && <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">{referenceShots.map((shot, index) => <div key={shot.shot_id} className="space-y-1"><MediaImage sessionId={sessionId} path={shot.selected || shot.path} alt={`镜头 ${index + 1} 的视频参考图`} /><p className="text-xs text-muted">镜头 {index + 1} → 片段 {index + 1}</p></div>)}</div>}
      <Button disabled={running || generationBlocked} onClick={() => void generate()}>{hasVideoOutput ? "使用参考图重新生成全部视频" : `使用 ${referenceShots.length} 张参考图生成视频`}</Button>
    </section>}
    <ModelSelector {...models} keys={modelKeys} selected={selection} disabled={running} onChange={value => void saveModels(value)} onRetry={models.retry} />
    {formatError && <p className="model-picker-error" role="alert">{formatError}</p>}
    <SessionKnowledge key={`${sessionId}:${(session.knowledge_library_ids ?? []).join(",")}`} libraryIds={session.knowledge_library_ids ?? []} disabled={running} onSave={saveKnowledge} />
    {session.knowledge_status?.changed && <div className="knowledge-change-note" role="status"><strong>创作资料已发生变化</strong><p>现有产物保留原资料版本。需要使用最新设定时，请主动重新生成剧本或分镜。</p>{session.knowledge_status.warnings?.map((warning, index) => <p key={index}>{warning}</p>)}</div>}
    {!session.knowledge_status?.changed && session.knowledge_status?.warnings?.map((warning, index) => <p className="knowledge-warning" key={index}>{warning}</p>)}
    {modelSaving ? <p className="model-saving-note" role="status">正在保存模型选择，保存成功后才能生成…</p> : running && <div className="space-y-2"><div className="h-1.5 rounded-full bg-surface-2 overflow-hidden"><div className="h-full bg-primary transition-all" style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} /></div><p className="text-sm text-muted" role="status">{message}（{percent}%）{waitingVideo && stepWaitSeconds >= 12 && !message.includes("已等待") ? ` · 此步骤已等待 ${stepWaitSeconds} 秒` : ""}</p>{waitingVideo && <p className="text-xs text-muted">已完成 {savedSegments}/{referenceShots.length} 个片段</p>}</div>}
    {(error || loadError || session.error) && <p className="text-sm text-danger" role="alert">{error || loadError || session.error}</p>}
    <RecoveryNotice key={`${session.execution?.execution_id ?? session.updated_at}:${session.status}`} kind="session" entityId={sessionId} status={session.status} revision={session.updated_at} busy={Boolean(running)} onResume={resume} />
    {stale && hasCurrentOutput && <p className="capability-note">内容已修改，请重新生成。</p>}
    {stage === "video_generation" && <div className="space-y-2"><label className="flex gap-3 items-center text-sm text-muted" htmlFor="video-mode">生成方式<select id="video-mode" className="version-select" disabled={running} value={videoMode || session.video_generation_mode} onChange={e => { setVideoMode(e.target.value); try { localStorage.setItem(`dream-machine:video-mode:${userId}:${sessionId}`, e.target.value); } catch {} }}><option value="first_frame">镜头首帧</option><option value="reference">多图参考</option><option value="start_end" disabled>首尾帧（需要配置尾帧）</option></select></label></div>}
    {versions.length > 0 && <div className="flex items-center gap-3 flex-wrap"><label className="text-sm text-muted" htmlFor="stage-version">阶段版本</label><select id="stage-version" className="version-select" value={currentVersion ?? ""} disabled={running} onChange={e => void modify({ operation: "select", stage_version_id: e.target.value })}>{!currentVersion && <option value="">当前调整（未作为版本）</option>}{versions.map((version, index) => <option key={version.version_id} value={version.version_id}>版本 {index + 1} · {new Date(version.created_at * 1000).toLocaleString("zh-CN")}</option>)}</select></div>}
    {stage === finalStage && versions.length > 0 && <details className="text-xs text-muted"><summary>下载历史成片</summary><div className="design-versions">{versions.map((version, index) => <button key={version.version_id} onClick={() => void downloadMedia(sessionId, `历史成片-版本${index + 1}.mp4`, version.version_id).catch(e => setError(e instanceof Error ? e.message : "历史成片下载失败"))}>下载历史版本 {index + 1}</button>)}</div></details>}
    {artifact ? stage === "script_generation" ? <ScriptView script={artifact as ScriptArtifact} /> : stage.startsWith("comic_") ? <ComicStageView key={stage} sessionId={sessionId} stage={stage} artifact={artifact} characters={characters} disabled={running} generationBlocked={blocked || !modelsReady} stale={stale || (stage === finalStage && Boolean(session.stale_stages?.length))} onSelect={(collection, id, path) => void modify({ operation: "select", selections: [{ collection, id, path }] })} onRegenerate={modifications => void modify(modifications)} /> : <StageView key={stage} projectType={projectType} sessionId={sessionId} userId={userId} versionId={currentVersion} stage={stage} artifact={artifact} stale={stale} disabled={running} generationBlocked={blocked || !modelsReady} onRegenerate={modifications => void modify({ ...modifications, ...(stage === "video_generation" ? { video_generation_mode: videoMode || session.video_generation_mode } : {}) })} onSelect={(collection, id, path) => void modify({ operation: "select", selections: [{ collection, id, path }] })} /> : !running && <p className="text-sm text-muted">{blocked ? "请先完成并确认前面的阶段。" : `点击生成${stageName}开始。`}</p>}
    {Boolean(artifact) && (stage === "script_generation" || stage === "storyboard") && <ArtifactEditor key={`${stage}:${currentVersion ?? "legacy"}`} artifact={artifact} userId={userId} sessionId={sessionId} stage={stage} versionId={currentVersion} design={session.artifacts.character_design as CharacterDesignArtifact | undefined} disabled={running} onSave={(artifact, regenerate) => modify({ operation: regenerate ? "regenerate" : "save", artifact })} />}
    {Boolean(artifact) && stage === "comic_storyboard" && <ComicStoryboardEditor key={`${stage}:${currentVersion ?? "current"}`} artifact={artifact as ComicStoryboardArtifact} characters={characters} design={session.artifacts.character_design as CharacterDesignArtifact | undefined} userId={userId} sessionId={sessionId} versionId={currentVersion} disabled={running} onSave={modify} />}
    {!running && <div className="flex gap-3 flex-wrap"><Button disabled={generationBlocked} onClick={() => void generate()}>{hasCurrentOutput ? `重新生成本阶段全部${stageName}` : `生成${stageName}`}</Button>{(canConfirm || canArchive) && <Button variant="secondary" onClick={() => void next()}>{canArchive ? "确认完成并归档" : `确认${stageName}，进入${STAGE_CN[stageOrder[stageOrder.indexOf(stage) + 1]] ?? "下一阶段"}`}</Button>}</div>}
    {session.status === "session_completed" && !session.stale_stages?.length && <p className="text-sm text-success">成片已归档到作品库，可预览、下载或继续修改。</p>}
  </div>;
}
