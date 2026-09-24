"use client";

import { useEffect, useState } from "react";
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

type View = "create" | "pipelines" | "settings" | "sandbox";

const NAV_ITEMS: { key: View; label: string; icon: typeof Clapperboard }[] = [
  { key: "create", label: "创作", icon: Clapperboard },
  { key: "pipelines", label: "短管线", icon: Zap },
  { key: "settings", label: "设置", icon: Settings },
  { key: "sandbox", label: "沙盒", icon: FlaskConical },
];

const STAGE_CN: Record<string, string> = {
  script_generation: "剧本",
  character_design: "角色场景",
  storyboard: "分镜",
  reference_generation: "参考图",
  video_generation: "视频",
  post_production: "成片",
};

const STATUS_CN: Record<string, string> = {
  idle: "未开始",
  running: "生成中",
  stage_completed: "可继续",
  session_completed: "已完成",
  failed: "失败",
};

function sessionTitle(s: SessionMeta): string {
  const script = s.artifacts?.script_generation as { title?: string } | undefined;
  return script?.title || s.idea.slice(0, 20);
}

export function Workspace() {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const sessionId = searchParams.get("session");
  const [view, setView] = useState<View>("create");
  const [formKey, setFormKey] = useState(0);
  const [sessions, setSessions] = useState<SessionMeta[] | null>(null);
  const [sessionsError, setSessionsError] = useState("");

  useEffect(() => {
    if (!loading && !user) {
      router.replace("/login");
    }
  }, [loading, user, router]);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    listSessions()
      .then((s) => {
        if (!cancelled) setSessions(s);
      })
      .catch((e) => {
        if (!cancelled) setSessionsError(e instanceof Error ? e.message : "加载失败");
      });
    return () => {
      cancelled = true;
    };
  }, [user]);

  function select(sid: string) {
    router.push(`/?session=${encodeURIComponent(sid)}`);
  }

  function newCreation() {
    router.push("/");
    setView("create");
    // 强制重建创作表单并聚焦创意输入框，保证点「新建」一定有可见反馈
    setFormKey((k) => k + 1);
  }

  async function reloadSessions() {
    setSessionsError("");
    try {
      setSessions(await listSessions());
    } catch (e) {
      setSessionsError(e instanceof Error ? e.message : "加载失败");
    }
  }

  function onCreated(s: SessionMeta) {
    void reloadSessions();
    select(s.session_id);
  }

  if (loading) {
    return (
      <main className="flex-1 flex items-center justify-center text-muted">加载中…</main>
    );
  }

  if (!user) {
    return null;
  }

  return (
    <div className="flex-1 flex min-h-0">
      {/* 侧栏 */}
      <aside className="w-16 lg:w-56 shrink-0 bg-surface border-r border-border flex flex-col">
        <div className="px-3 lg:px-5 py-4 border-b border-border">
          <span className="hidden lg:block text-lg font-bold bg-gradient-to-r from-primary to-accent bg-clip-text text-transparent">
            AI造梦机
          </span>
          <span className="lg:hidden text-lg font-bold text-primary">🎬</span>
        </div>

        <nav className="flex-1 py-3 space-y-1">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = view === item.key;
            return (
              <button
                key={item.key}
                onClick={() => setView(item.key)}
                aria-label={item.label}
                aria-current={active ? "page" : undefined}
                title={item.label}
                className={`w-full flex items-center gap-3 px-3 lg:px-5 py-2.5 text-sm transition-colors ${
                  active
                    ? "bg-primary/10 text-primary font-medium"
                    : "text-muted hover:bg-surface-2 hover:text-foreground"
                }`}
              >
                <Icon size={18} />
                <span className="hidden lg:inline">{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="border-t border-border p-3 lg:p-4 space-y-2">
          <div className="hidden lg:block text-xs text-muted truncate">
            用户 {user.user_id.slice(0, 8)}…
          </div>
          <button
            onClick={() => void logout()}
            aria-label="退出"
            title="退出"
            className="w-full flex items-center justify-center lg:justify-start gap-2 text-sm text-muted hover:text-danger transition-colors"
          >
            <LogOut size={16} />
            <span className="hidden lg:inline">退出</span>
          </button>
        </div>
      </aside>

      {/* 主内容 */}
      <main className="flex-1 min-w-0 overflow-auto">
        {view === "create" && (
          <div className="flex flex-col lg:flex-row gap-4 p-4 lg:p-6">
            <div className="w-full lg:w-72 shrink-0 bg-surface rounded-xl border border-border shadow-sm p-4">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-medium text-foreground">我的作品</h2>
                <button
                  className="flex items-center gap-1 text-sm text-primary hover:underline"
                  onClick={newCreation}
                >
                  <Plus size={14} />
                  新建
                </button>
              </div>
              {sessions === null && !sessionsError && (
                <p className="text-sm text-muted">加载中…</p>
              )}
              {sessionsError && (
                <div className="text-sm text-danger space-y-2">
                  <p>{sessionsError}</p>
                  <button className="text-primary hover:underline" onClick={() => void reloadSessions()}>
                    重试
                  </button>
                </div>
              )}
              {sessions?.length === 0 && <p className="text-sm text-muted">暂无作品</p>}
              <ul className="space-y-1">
                {sessions?.map((s) => {
                  const active = s.session_id === sessionId;
                  return (
                    <li key={s.session_id}>
                      <button
                        type="button"
                        onClick={() => select(s.session_id)}
                        aria-current={active ? "true" : undefined}
                        className={`w-full text-left text-sm rounded-lg px-3 py-2 border ${
                          active
                            ? "border-primary bg-primary/5"
                            : "border-transparent hover:border-border hover:bg-surface-2"
                        }`}
                      >
                        <div className="text-foreground truncate">{sessionTitle(s)}</div>
                        <div className="text-xs text-muted mt-0.5">
                          <span className="text-accent">
                            {STAGE_CN[s.current_stage ?? ""] ?? "未开始"}
                          </span>
                          {" · "}
                          {STATUS_CN[s.status] ?? s.status}
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>

            <div className="flex-1 bg-surface rounded-xl border border-border shadow-sm p-6 min-w-0">
              <CreationPanel
                key={`${sessionId ?? "new"}:${formKey}`}
                sessionId={sessionId}
                onCreated={onCreated}
              />
            </div>
          </div>
        )}

        {view === "pipelines" && (
          <div className="p-4 lg:p-6">
            <div className="bg-surface rounded-xl border border-border shadow-sm p-6">
              <PipelinesPanel />
            </div>
          </div>
        )}

        {view === "settings" && (
          <div className="p-4 lg:p-6">
            <div className="bg-surface rounded-xl border border-border shadow-sm p-6">
              <SettingsPanel />
            </div>
          </div>
        )}

        {view === "sandbox" && (
          <div className="p-4 lg:p-6">
            <div className="bg-surface rounded-xl border border-border shadow-sm p-6">
              <SandboxPanel />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
