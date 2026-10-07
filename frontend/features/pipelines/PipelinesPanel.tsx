"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { createTask, downloadTask, getTask, listTasks, streamTask, uploadFile, type TaskMeta } from "@/lib/api/tasks";
import { TaskVideo } from "@/features/cinema/SessionLibrary";
import { modelSelectionError, resolvedModelSelection, STATUS_LABELS, TASK_TO_TOOL, TOOL_LABELS, TALKING_MODE_LABELS, talkingModeOf, talkingScriptLength, taskCanPreview, taskLabel, taskResultError, taskIsActive, toolDraftFromTask, toolTaskInput, toolModelKeys, type PipelineTab, type ToolDraft, type TalkingMode } from "@/lib/workflow";
import { AssetPicker } from "./AssetPicker";
import { useModelCatalog } from "@/lib/useModelCatalog";
import { ModelSelector } from "@/features/creation/ModelSelector";
import { RecoveryNotice } from "@/features/creation/RecoveryNotice";
import { resumeTask } from "@/lib/api/recovery";
export type { PipelineTab } from "@/lib/workflow";

const inputCls = "w-full px-3 py-2 rounded-md bg-surface-2 border border-border text-foreground text-sm placeholder:text-muted focus:outline-none focus:border-primary";
const TASK_TYPES: Record<PipelineTab, string> = { literary: "literary_video", motion: "motion_transfer", talking: "talking_head" };

export function PipelinesPanel({ tab, taskId, userId, draft, onDraftChange, onTabChange, onOpenTask, onCreated }: {
  tab: PipelineTab; taskId?: string; userId: string; draft: ToolDraft; onDraftChange: (draft: ToolDraft) => void;
  onTabChange: (tab: PipelineTab) => void; onOpenTask: (task: TaskMeta) => void; onCreated: (taskId: string) => void;
}) {
  const [tasks, setTasks] = useState<TaskMeta[]>([]);
  const [task, setTask] = useState<TaskMeta | null>(null);
  const [msg, setMsg] = useState("");
  const [loadError, setLoadError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [editing, setEditing] = useState(!taskId);
  const latestDraft = useRef(draft);
  const resumeInFlight = useRef(false);
  const resumeController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  useEffect(() => { latestDraft.current = draft; }, [draft]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; resumeController.current?.abort(); }; }, []);
  const busy = submitting || uploading;
  const patch = (value: Partial<ToolDraft>) => onDraftChange({ ...draft, ...value });
  const models = useModelCatalog();
  const talkingMode = talkingModeOf(draft.talkingMode);
  const modelKeys = toolModelKeys(tab, talkingMode);
  const selection = resolvedModelSelection(draft.modelSelection, models.catalog, modelKeys);
  const modelError = modelSelectionError(selection, models.catalog, modelKeys);
  const modelsReady = !modelKeys.length || (!models.loading && !models.error && !modelError);
  const activeTask = Boolean(task && taskIsActive(task.status));
  const resultError = task ? taskResultError(task) : "";
  const scriptLength = talkingScriptLength(draft.text);
  const scriptTooLong = tab === "talking" && talkingMode === "lip_sync" && scriptLength > 40;

  const reloadTasks = useCallback(async () => {
    try { setTasks(await listTasks()); } catch (e) { setMsg(e instanceof Error ? e.message : "任务加载失败"); }
  }, []);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      if (cancelled) return;
      try {
        const records = await listTasks();
        if (cancelled) return;
        setTasks(records);
        if (taskId) {
          const current = records.find(t => t.task_id === taskId) ?? await getTask(taskId);
          if (!cancelled) setTask(current);
        }
        setLoadError("");
      } catch (e) { if (!cancelled) setLoadError(e instanceof Error ? e.message : "任务加载失败，请刷新重试"); }
      if (!cancelled) timer = setTimeout(refresh, 2500);
    };
    void refresh();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [taskId]);

  async function upload(file: File | undefined) {
    if (!file) return;
    setUploading(true); setMsg("");
    try {
      const filename = await uploadFile(file);
      onDraftChange({ ...latestDraft.current, image: filename, imageName: file.name, assetSource: "" });
    } catch (e) { setMsg(`${file.name} 未上传成功，需重新选择。${e instanceof Error ? e.message : ""}`); }
    finally { setUploading(false); }
  }

  async function submit() {
    if (busy || activeTask) return;
    if (!modelsReady) return setMsg(models.error || modelError || "模型目录加载中，请稍候。");
    if (!draft.text.trim()) return setMsg("请填写文案或动作描述");
    if (tab !== "literary" && !draft.image) return setMsg("请上传或选择一张人物图");
    if (scriptTooLong) return setMsg("人物嘴型同步每次生成 10 秒视频，文案最多 40 个非空白字符，请缩短后重试。");
    if (tab === "literary" && !draft.style.trim()) return setMsg("请填写视觉风格");
    setSubmitting(true); setMsg("");
    const chosenDraft = { ...draft, ...(modelKeys.length ? { modelSelection: { ...draft.modelSelection, ...selection } } : {}) };
    onDraftChange(chosenDraft);
    const input = toolTaskInput(tab, chosenDraft);
    // A manually retried request with an uncertain network outcome reuses its key.
    const storageKey = `dream-machine:submission:${encodeURIComponent(userId)}:${tab}`;
    const fingerprint = JSON.stringify(input);
    let idempotencyKey = crypto.randomUUID();
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) ?? "null") as { fingerprint?: string; key?: string } | null;
      if (saved?.fingerprint === fingerprint && saved.key) idempotencyKey = saved.key;
      localStorage.setItem(storageKey, JSON.stringify({ fingerprint, key: idempotencyKey }));
    } catch { /* Request still has a key when storage is unavailable. */ }
    try {
      const { task_id } = await createTask(TASK_TYPES[tab], input, idempotencyKey);
      try { localStorage.removeItem(storageKey); } catch {}
      setMsg("已提交，后台生成中。离开页面后仍可在作品库查看。");
      // Preserve the input after success as well, so continuing/retrying never needs retyping.
      await reloadTasks(); onCreated(task_id);
    } catch (e) { setMsg(e instanceof Error ? e.message : "提交失败，草稿已保留"); }
    finally { setSubmitting(false); }
  }

  function useTaskInput() {
    if (!task) return;
    onDraftChange(toolDraftFromTask(task));
    setEditing(true); setMsg("已恢复这次创作的输入。修改后点击生成，会创建新的快捷作品。");
  }

  async function resume(executionId: string): Promise<void> {
    if (!task || busy || activeTask || resumeInFlight.current) return;
    resumeInFlight.current = true;
    setSubmitting(true); setMsg("正在继续原来的生成任务…");
    resumeController.current = new AbortController();
    try {
      const resumed = await resumeTask(task.task_id, executionId);
      if (!mounted.current) return;
      setTask(await getTask(resumed.task_id));
      await streamTask(resumed.task_id, event => {
        if (!mounted.current) return;
        if (event.type === "progress") setMsg(`${event.message}（${event.percent}%）`);
        else if (event.type === "error") setMsg(event.error.message);
        else if (event.type === "done") setMsg("后台任务已更新，请查看作品状态。");
      }, resumeController.current.signal);
    } catch (e) { if (mounted.current && !(e instanceof Error && e.name === "AbortError")) setMsg(e instanceof Error ? e.message : "继续失败，请查看作品状态后重试。"); }
    finally {
      resumeInFlight.current = false;
      if (mounted.current) {
        try { setTask(await getTask(task.task_id)); await reloadTasks(); }
        catch (e) { setLoadError(e instanceof Error ? e.message : "任务状态读取失败"); }
        setSubmitting(false);
      }
    }
  }

  return <div className="space-y-6 max-w-3xl">
    <div><h2 className="text-base font-semibold text-foreground mb-3">快捷短片 · 一次性出片</h2><div className="flex gap-2 flex-wrap">{(Object.keys(TOOL_LABELS) as PipelineTab[]).map(tool => <Button key={tool} variant={tab === tool ? "primary" : "secondary"} onClick={() => onTabChange(tool)}>{TOOL_LABELS[tool]}</Button>)}</div></div>
    {loadError && <p className="text-sm text-danger" role="alert">{loadError}</p>}
    {taskId && !task && !loadError && <p className="text-sm text-muted">正在恢复作品与生成状态…</p>}
    {task && <section className="task-detail border border-border rounded-lg p-4 space-y-3">
      <div className="flex gap-3 items-center flex-wrap"><h3>{TOOL_LABELS[TASK_TO_TOOL[task.type]] ?? task.type}</h3><span className={`work-status${resultError ? " error" : ""}`}>{resultError ? "结果异常" : STATUS_LABELS[task.status] ?? task.status}</span></div>
      {task.type === "talking_head" && <p className="text-sm text-muted">生成模式：{TALKING_MODE_LABELS[talkingModeOf(task.input.talking_mode)]}</p>}
      <p className="text-sm text-muted">{String(task.input.text ?? task.input.script ?? task.input.prompt ?? "")}</p>
      {taskIsActive(task.status) && <p className="text-sm text-muted" role="status">{task.execution?.last_event?.message || "后台正在生成，页面会自动更新进度。"}{typeof task.execution?.last_event?.percent === "number" ? `（${task.execution.last_event.percent}%）` : ""}</p>}
      {task.error && <p className="text-sm text-danger" role="alert">{task.error}</p>}
      {resultError && <p className="text-sm text-danger" role="alert">{resultError}</p>}
      <RecoveryNotice key={`${task.execution?.execution_id ?? task.updated_at}:${task.status}`} kind="task" entityId={task.task_id} status={task.status} revision={task.updated_at} busy={busy || activeTask} onResume={resume} />
      {taskCanPreview(task) && <><TaskVideo key={task.task_id} taskId={task.task_id} /><Button variant="secondary" onClick={() => void downloadTask(task.task_id).catch(e => setMsg(e instanceof Error ? e.message : "下载失败"))}>下载成片</Button></>}
      {!taskIsActive(task.status) && <Button variant="secondary" onClick={useTaskInput}>用这份输入继续创作</Button>}
    </section>}
    {editing && <div className="space-y-4">
      <p className="text-xs text-muted">草稿自动保存在当前浏览器，按账号隔离。故事的剧集数与比例不适用于当前快捷工具，成片使用工具默认规格。</p>
      {tab === "talking" && <>
        <fieldset disabled={busy || activeTask} className="space-y-2"><legend className="text-sm text-foreground mb-2">成片模式</legend><div className="flex gap-4 flex-wrap">{(Object.keys(TALKING_MODE_LABELS) as TalkingMode[]).map(mode => <label key={mode} className="flex items-center gap-2 text-sm"><input type="radio" name="talking-mode" value={mode} checked={talkingMode === mode} onChange={() => patch({ talkingMode: mode })} />{TALKING_MODE_LABELS[mode]}</label>)}</div></fieldset>
      </>}
      {tab !== "literary" && <div><label htmlFor={tab === "motion" ? "motion-char" : "talking-person"} className="block text-sm text-muted mb-1">人物图</label><input id={tab === "motion" ? "motion-char" : "talking-person"} type="file" accept="image/*" disabled={busy} onChange={e => void upload(e.target.files?.[0])} />{draft.image && <p className="text-xs text-muted mt-1">已保存：{draft.imageName || draft.image}<button className="text-link ml-3" onClick={() => patch({ image: "", imageName: "", assetSource: "" })}>移除</button></p>}<AssetPicker disabled={busy} onSelected={(image, imageName, assetSource) => patch({ image, imageName, assetSource })} /></div>}
      <div><label htmlFor={tab === "literary" ? "lit-text" : tab === "motion" ? "motion-prompt" : "talking-script"} className="block text-sm text-muted mb-1">{tab === "literary" ? "文案 / 灵感" : tab === "motion" ? "动作描述" : "口播文案"}</label><textarea id={tab === "literary" ? "lit-text" : tab === "motion" ? "motion-prompt" : "talking-script"} value={draft.text} onChange={e => patch({ text: e.target.value })} rows={4} className={`${inputCls} resize-y`} /></div>
      {tab === "talking" && talkingMode === "lip_sync" && <p className={`text-xs ${scriptTooLong ? "text-danger" : "text-muted"}`} role={scriptTooLong ? "alert" : undefined}>{scriptLength}/40 个非空白字符{scriptTooLong ? "，请缩短文案后生成。" : " · 10 秒素材，自动裁去静音尾段"}</p>}
      {tab === "literary" && <div><label htmlFor="lit-style" className="block text-sm text-muted mb-1">风格</label><Input id="lit-style" value={draft.style} onChange={e => patch({ style: e.target.value })} /></div>}
      <ModelSelector {...models} keys={modelKeys} selected={selection} disabled={busy || activeTask} onChange={modelSelection => patch({ modelSelection: { ...draft.modelSelection, ...modelSelection } })} onRetry={models.retry} />
      <Button disabled={busy || activeTask || !modelsReady || scriptTooLong} onClick={() => void submit()}>{uploading ? "上传中…" : submitting ? "提交中…" : "生成快捷短片"}</Button>
    </div>}
    {msg && <p className="text-sm text-muted" role="status">{msg}</p>}
    <div className="border-t border-border pt-4"><h3 className="text-sm font-medium text-foreground mb-3">近期快捷作品</h3>{!tasks.length ? <p className="text-sm text-muted">暂无快捷作品</p> : <ul className="space-y-2">{tasks.slice(0, 8).map(t => <li key={t.task_id} className="border border-border rounded-lg"><button className="task-list-open" onClick={() => onOpenTask(t)}><span>{taskLabel(t)}</span><span className={t.status === "failed" || taskResultError(t) ? "text-danger" : "text-muted"}>{taskResultError(t) ? "结果异常" : STATUS_LABELS[t.status] ?? t.status}</span><span className="text-muted">查看 ↗</span></button></li>)}</ul>}</div>
  </div>;
}
