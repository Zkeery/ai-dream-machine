"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Clapperboard,
  FlaskConical,
  LogOut,
  Plus,
  Settings,
  Zap,
} from "lucide-react";
import { useAuth } from "@/features/auth/AuthProvider";
import { CreationPanel } from "@/features/creation/CreationPanel";
import { PipelinesPanel } from "@/features/pipelines/PipelinesPanel";
import { SandboxPanel } from "@/features/sandbox/SandboxPanel";
import { SettingsPanel } from "@/features/settings/SettingsPanel";
import { listSessions, type SessionMeta } from "@/lib/api/sessions";
import { CinemaHome } from "@/features/cinema/CinemaHome";
import { SessionLibrary, sessionTitle } from "@/features/cinema/SessionLibrary";
import type { PipelineTab } from "@/features/pipelines/PipelinesPanel";
import { listTasks, type TaskMeta } from "@/lib/api/tasks";
import { carryStoryToTool, parseRoute, PROJECT_LABELS, projectTypeOf, routeUrl, TASK_TO_TOOL, type WorkspaceRoute, type WorkspaceView } from "@/lib/workflow";
import { useWorkspaceDraft } from "@/lib/useWorkspaceDraft";
import { KnowledgePanel } from "@/features/knowledge/KnowledgePanel";
import { UsageSummary } from "./UsageSummary";

type View = WorkspaceView;

const NAV_ITEMS: { key: View; label: string; icon: typeof Clapperboard }[] = [
  { key: "create", label: "创作", icon: Clapperboard },
  { key: "pipelines", label: "短管线", icon: Zap },
  { key: "settings", label: "设置", icon: Settings },
  { key: "sandbox", label: "沙盒", icon: FlaskConical },
];

export function Workspace({ userOnly = false }: { userOnly?: boolean } = {}) {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  useEffect(() => { if (!loading && !user) router.replace(userOnly ? "/login?next=/user" : "/login"); }, [loading, user, router, userOnly]);
  if (loading) return <main className="flex-1 flex items-center justify-center text-muted">加载中…</main>;
  if (!user) return null;
  return <AccountWorkspace key={`${user.user_id}:${userOnly}`} userId={user.user_id} isAdmin={!userOnly && user.is_admin === true} userOnly={userOnly} logout={logout} />;
}

function AccountWorkspace({ userId, isAdmin, userOnly, logout }: { userId: string; isAdmin: boolean; userOnly: boolean; logout: () => Promise<void> }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { draft, updateDraft, storageError } = useWorkspaceDraft(userId);
  const requestedRoute = parseRoute(searchParams, draft.tool);
  const route: WorkspaceRoute = userOnly && (requestedRoute.view === "settings" || requestedRoute.view === "sandbox") ? { view: "create" } : requestedRoute;
  const { view, sessionId } = route;
  const [formKey, setFormKey] = useState(0);
  const [sessions, setSessions] = useState<SessionMeta[] | null>(null);
  const [tasks, setTasks] = useState<TaskMeta[] | null>(null);
  const [sessionsError, setSessionsError] = useState("");
  const activeSession = sessions?.find(s => s.session_id === sessionId);
  const projectType = activeSession ? projectTypeOf(activeSession) : route.projectType ?? "story";
  const navigate = (next: WorkspaceRoute) => router.push(userOnly ? `/user${routeUrl(next).slice(1)}` : routeUrl(next));
  const setView = (view: View) => navigate({ view, ...(view === "pipelines" ? { tool: draft.tool } : {}) });

  const reloadSessions = useCallback(async () => {
    const [stories, shortcuts] = await Promise.allSettled([listSessions(), listTasks()]);
    const errors: string[] = [];
    if (stories.status === "fulfilled") setSessions(stories.value);
    else errors.push(stories.reason instanceof Error ? stories.reason.message : "故事加载失败");
    if (shortcuts.status === "fulfilled") setTasks(shortcuts.value);
    else errors.push(shortcuts.reason instanceof Error ? shortcuts.reason.message : "快捷作品加载失败");
    setSessionsError(errors.join("；"));
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      if (cancelled) return;
      await reloadSessions();
      if (!cancelled) timer = setTimeout(refresh, 4000);
    };
    void refresh();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [reloadSessions]);

  function select(sid: string) {
    const type = sessions?.find(s => s.session_id === sid)?.project_type;
    navigate({ view: "create", sessionId: sid, ...(type === "comic" ? { projectType: "comic" } : {}) });
  }

  function showHome() {
    navigate({ view: "create" });
  }

  function openPipeline(tab: PipelineTab, idea: string) {
    updateDraft(current => carryStoryToTool({ ...current, [projectType]: { ...current[projectType], idea } }, tab, projectType));
    navigate({ view: "pipelines", tool: tab });
    window.scrollTo(0, 0);
  }

  function newCreation() {
    navigate({ view: "create", ...(projectType === "comic" ? { projectType: "comic" } : {}) });
    // 强制重建创作表单并聚焦创意输入框，保证点「新建」一定有可见反馈
    setFormKey((k) => k + 1);
  }

  function selectTask(task: TaskMeta) { navigate({ view: "pipelines", tool: TASK_TO_TOOL[task.type] ?? "literary", taskId: task.task_id }); }

  function onCreated(s: SessionMeta) {
    void reloadSessions();
    navigate({ view: "create", sessionId: s.session_id, ...(s.project_type === "comic" ? { projectType: "comic" } : {}) });
  }

  function onDeleted(sid: string) {
    setSessions(current => current?.filter(session => session.session_id !== sid) ?? null);
    if (sessionId === sid) navigate({ view: "projects" });
  }

  return (
    <div className="night-studio">
      <div className="shell">
        <header className="header">
          <button className="brand" onClick={showHome} aria-label="AI造梦机首页">
            <svg viewBox="0 0 32 38" aria-hidden="true"><path d="M8 32c-5-3-7-9-4-15C6 10 13 7 20 9s11 8 8 14c-2 5-8 8-13 6M9 33l11-17M8 23c0-5 5-9 10-8s8 6 5 10M8 7c5-4 12-4 17 1" /></svg>
            <span className="brand-name">AI造梦机</span><span className="brand-en">DREAM MACHINE</span>
          </button>
          <nav className="nav" aria-label="主导航">
            <button className={view === "create" ? "active" : ""} onClick={newCreation} aria-current={view === "create" ? "page" : undefined}>创作台</button>
            <button className={view === "projects" ? "active" : ""} onClick={() => setView("projects")} aria-current={view === "projects" ? "page" : undefined}>我的作品</button>
            <button className={view === "pipelines" ? "active" : ""} onClick={() => setView("pipelines")} aria-current={view === "pipelines" ? "page" : undefined}>短视频工具</button>
            <button className={view === "knowledge" ? "active" : ""} onClick={() => setView("knowledge")} aria-label="创作知识库" aria-current={view === "knowledge" ? "page" : undefined}>知识库</button>
          </nav>
          <div className="header-actions">
            <div className="utility-nav">
              {isAdmin && NAV_ITEMS.filter(item => item.key === "settings" || item.key === "sandbox").map(item => (
                <button key={item.key} className={view === item.key ? "active" : ""} onClick={() => setView(item.key)} aria-label={item.label} title={item.label} aria-current={view === item.key ? "page" : undefined}><item.icon size={16} /></button>
              ))}
              <button onClick={() => void logout()} aria-label="退出登录" title="退出登录"><LogOut size={16} /></button>
            </div>
          </div>
        </header>
        <main>
          {isAdmin && <UsageSummary />}
          {storageError && <p className="composer-error" role="alert">{storageError}</p>}
          {view === "create" && !sessionId && <CinemaHome
            key={formKey} onCreated={onCreated} onPipeline={openPipeline} focusOnMount={formKey > 0}
            sessions={sessions} sessionsError={sessionsError} onReload={() => void reloadSessions()}
            onSelectSession={select} onProjects={() => setView("projects")}
            tasks={tasks} onSelectTask={selectTask} draft={draft[projectType]} onDraftChange={projectDraft => updateDraft(current => ({ ...current, [projectType]: projectDraft }))}
            projectType={projectType} onProjectTypeChange={type => navigate({ view: "create", ...(type === "comic" ? { projectType: "comic" } : {}) })}
            onKnowledge={() => setView("knowledge")}
          />}
          {view === "create" && sessionId && <section className="studio-project">
            <div className="studio-section-header"><div><p className="section-kicker">{PROJECT_LABELS[projectType]} / WORKSPACE</p><h1>{activeSession ? sessionTitle(activeSession) : "我的作品"}</h1></div><button className="text-link" onClick={() => navigate({ view: "create", ...(projectType === "comic" ? { projectType: "comic" } : {}) })}>返回创作台 ↗</button></div>
            <div className="studio-work-grid">
              <aside className="studio-panel session-sidebar"><h2>我的作品</h2><SessionLibrary sessions={sessions} error={sessionsError} onReload={() => void reloadSessions()} onSelect={select} onDeleted={onDeleted} selectedId={sessionId} /></aside>
              <div className="studio-panel stage-workspace"><CreationPanel key={sessionId} sessionId={sessionId ?? null} userId={userId} onCreated={onCreated} onChanged={() => void reloadSessions()} /></div>
            </div>
          </section>}
          {view === "projects" && <section className="studio-section">
            <div className="studio-section-header"><div><p className="section-kicker">YOUR STORIES, IN PROGRESS</p><h1>我的作品</h1><p>故事短视频、漫剧与快捷短片，随时继续。</p></div><button className="cinema-primary" onClick={newCreation}><Plus size={14} />新建创作</button></div>
            <SessionLibrary sessions={sessions} tasks={tasks} error={sessionsError} onReload={() => void reloadSessions()} onSelect={select} onSelectTask={selectTask} onDeleted={onDeleted} />
          </section>}
          {view === "pipelines" && <section className="studio-section"><div className="studio-section-header"><div><p className="section-kicker">SHORT VIDEO TOOLS</p><h1>快速做一支短视频。</h1><p>文艺短视频、角色动作短片、图片配音口播，三个独立工具，专用于短视频创作。</p></div><button className="text-link" onClick={() => setView("projects")}>查看全部作品 ↗</button></div><div className="studio-panel"><PipelinesPanel key={`${route.tool}:${route.taskId ?? "draft"}`} tab={route.tool ?? draft.tool} taskId={route.taskId} userId={userId} draft={draft.tools[route.tool ?? draft.tool]} onDraftChange={toolDraft => updateDraft(current => ({ ...current, tools: { ...current.tools, [route.tool ?? current.tool]: toolDraft } }))} onTabChange={tool => { updateDraft(current => ({ ...current, tool })); navigate({ view: "pipelines", tool }); }} onOpenTask={selectTask} onCreated={taskId => { void reloadSessions(); navigate({ view: "pipelines", tool: route.tool, taskId }); }} /></div></section>}
          {isAdmin && view === "settings" && <section className="studio-section"><div className="studio-section-header"><div><p className="section-kicker">ADMIN / STUDIO SETTINGS</p><h1>工作室设置</h1></div></div><div className="studio-panel"><SettingsPanel /></div></section>}
          {isAdmin && view === "sandbox" && <section className="studio-section"><div className="studio-section-header"><div><p className="section-kicker">ADMIN / SYSTEM SANDBOX</p><h1>系统沙盒</h1></div></div><div className="studio-panel"><SandboxPanel /></div></section>}
          {!isAdmin && (view === "settings" || view === "sandbox") && <section className="studio-section"><div className="studio-panel library-state" role="alert"><h1>此页面仅供管理员使用</h1><p>前往创作台，继续你的故事。</p><button className="cinema-primary" onClick={newCreation}>返回创作台</button></div></section>}
          {view === "knowledge" && <KnowledgePanel key={`${userId}:${route.libraryId ?? "overview"}`} libraryId={route.libraryId} onSelectLibrary={libraryId => navigate({ view: "knowledge", libraryId })} />}
        </main>
        <footer className="page-footer"><span>AI造梦机 · 从一句想象，到一部电影。</span><span>夜幕影院 / CREATIVE STUDIO</span></footer>
      </div>
    </div>
  );
}
