"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Clapperboard, RefreshCw, Trash2 } from "lucide-react";
import { deleteSession, type SessionMeta } from "@/lib/api/sessions";
import { downloadTask, loadTaskVideo, type TaskMeta } from "@/lib/api/tasks";
import { finalStageOf, finalVideoOf, libraryItems, libraryMatchesFilter, STATUS_LABELS, taskCanPreview, taskResultError, titleOfSession, type LibraryItem } from "@/lib/workflow";
import { downloadMedia } from "@/lib/media";
import { MediaVideo } from "@/features/creation/media";

export const STAGE_CN: Record<string, string> = {
  script_generation: "剧本", character_design: "角色场景", storyboard: "分镜", reference_generation: "参考", video_generation: "视频", post_production: "成片",
  comic_storyboard: "镜头对白", comic_panels: "漫画镜头", comic_audio: "角色配音", comic_composition: "漫剧成片",
};
export const STATUS_CN = STATUS_LABELS;
export const sessionTitle = titleOfSession;

export function TaskVideo({ taskId }: { taskId: string }) {
  const [src, setSrc] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false, url = "";
    loadTaskVideo(taskId).then(value => {
      if (cancelled) URL.revokeObjectURL(value);
      else { url = value; setSrc(value); }
    }).catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : "预览失败"); });
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [taskId]);
  if (error) return <p className="text-sm text-danger" role="alert">{error}</p>;
  return src ? <video src={src} controls className="rounded-lg max-w-full aspect-video bg-black" /> : <p className="text-sm text-muted">加载预览…</p>;
}

export function SessionLibrary({ sessions, tasks, error, onReload, onSelect, onSelectTask, compact = false, selectedId, onDeleted }: {
  sessions: SessionMeta[] | null;
  tasks?: TaskMeta[] | null;
  error: string;
  onReload: () => void;
  onSelect: (id: string) => void;
  onSelectTask?: (task: TaskMeta) => void;
  compact?: boolean;
  selectedId?: string;
  onDeleted?: (sessionId: string) => void;
}) {
  const [filter, setFilter] = useState("all");
  const [preview, setPreview] = useState<LibraryItem | null>(null);
  const [actionError, setActionError] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deletedIds, setDeletedIds] = useState<Set<string>>(() => new Set());
  const [actionMessage, setActionMessage] = useState("");
  const deleting = useRef(false);
  if (error) return <div className="library-state" role="alert"><p>{error}</p><button className="text-link" onClick={onReload}><RefreshCw size={13} />重新加载</button></div>;
  if (!sessions || tasks === null) return <div className="library-state" role="status">正在加载你的作品…</div>;
  const allItems = libraryItems(sessions.filter(session => !deletedIds.has(session.session_id)), tasks ?? []);
  const filtered = allItems.filter(item => libraryMatchesFilter(item, filter));
  const items = compact ? filtered.slice(0, 3) : filtered;
  async function download(item: LibraryItem) {
    setActionError("");
    try { if (item.kind === "session") await downloadMedia(item.id); else await downloadTask(item.id); }
    catch (e) { setActionError(e instanceof Error ? e.message : "下载失败"); }
  }
  async function remove(item: LibraryItem) {
    if (!item.session || deleting.current) return;
    if (!window.confirm(`删除「${item.title}」？\n删除后作品将从作品库移除，无法继续编辑。已被其他作品复用的素材和知识库不受影响。`)) return;
    deleting.current = true;
    setDeletingId(item.id); setActionError(""); setActionMessage("");
    try {
      await deleteSession(item.id);
      setDeletedIds(current => new Set([...current, item.id]));
      setPreview(current => current?.kind === "session" && current.id === item.id ? null : current);
      setActionMessage(`已删除「${item.title}」。`);
      onDeleted?.(item.id);
      onReload();
    } catch (e) { setActionError(e instanceof Error ? e.message : "作品删除失败，请重试"); }
    finally { deleting.current = false; setDeletingId(null); }
  }
  return <>
    {!compact && tasks !== undefined && <div className="library-filters" aria-label="作品类型">
      {[["all", "全部作品"], ["story", "故事短视频"], ["comic", "漫剧"], ["task", "快捷短片"]].map(([value, label]) => <button key={value} className={filter === value ? "active" : ""} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>)}
      <button className="text-link" onClick={onReload}><RefreshCw size={12} />刷新</button>
    </div>}
    {actionError && <p className="text-sm text-danger" role="alert">{actionError}</p>}
    {actionMessage && <p className="work-action-message" role="status">{actionMessage}</p>}
    {!items.length ? <div className="library-state"><Clapperboard size={21} /><div><p>{allItems.length ? "这个分类还没有作品。" : "你的第一部作品，从一句想象开始。"}</p><small>故事、漫剧与快捷短片都会保存在这里，随时继续制作。</small></div></div> : <div className={compact ? "work-cards" : "work-cards full"}>
      {items.map((item, i) => {
        const s = item.session;
        const canPreview = item.kind === "task" ? Boolean(item.task && taskCanPreview(item.task)) : Boolean(s && finalVideoOf(s)) && !s?.stale_stages?.length;
        return <article key={item.key} className={"work-card" + (s?.session_id === selectedId ? " active" : "")}>
          <button className="work-open" disabled={deletingId === item.id && item.kind === "session"} onClick={() => s ? onSelect(s.session_id) : item.task && onSelectTask?.(item.task)} aria-label={`打开${item.title}`}>
            <div className="work-card-top"><span className="work-number">{String(i + 1).padStart(2, "0")}</span><span className={"work-status" + (item.status === "failed" ? " error" : "")}>{STATUS_LABELS[item.status] ?? item.status}</span><ArrowUpRight size={14} /></div>
            <h3>{item.title}</h3><p>{item.description}</p>
            <div className="work-card-bottom"><span>{item.source}{s ? ` · ${STAGE_CN[s.current_stage ?? finalStageOf(s)] ?? "成片"} · ${s.video_ratio}` : ""}</span><span>{new Date(item.updatedAt * 1000).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })}</span></div>
          </button>
          {!compact && <div className="work-card-actions">
            <button disabled={deletingId === item.id && item.kind === "session"} onClick={() => s ? onSelect(s.session_id) : item.task && onSelectTask?.(item.task)}>{item.status === "running" || item.status === "pending" ? "查看进度" : "打开 / 继续"}</button>
            {canPreview && <><button onClick={() => setPreview(item)}>预览成片</button><button onClick={() => void download(item)}>下载</button></>}
            {s && <button type="button" className="work-delete" disabled={Boolean(deletingId)} aria-label={`删除${item.title}`} onClick={e => { e.preventDefault(); e.stopPropagation(); void remove(item); }}><Trash2 size={12} />{deletingId === item.id ? "删除中…" : "删除"}</button>}
          </div>}
          {!compact && (s?.error || item.task?.error) && <p className="work-error">{s?.error || item.task?.error}</p>}
          {!compact && item.task && taskResultError(item.task) && <p className="work-error">{taskResultError(item.task)}</p>}
          {!compact && Boolean(s?.stale_stages?.length) && <p className="work-error">上游内容已修改，下游需重新生成。</p>}
        </article>;
      })}
    </div>}
    {preview && <section className="library-preview studio-panel"><div className="flex justify-between gap-3 mb-3"><h3>{preview.title}</h3><button className="text-link" onClick={() => setPreview(null)}>关闭预览</button></div>{preview.kind === "task" ? <TaskVideo key={preview.id} taskId={preview.id} /> : preview.session && <MediaVideo key={preview.id} sessionId={preview.id} path={finalVideoOf(preview.session)} />}</section>}
  </>;
}
