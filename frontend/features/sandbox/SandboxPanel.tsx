"use client";

import { useEffect, useState } from "react";
import { listSessions } from "@/lib/api/sessions";
import { listTasks } from "@/lib/api/tasks";

export function SandboxPanel() {
  const [health, setHealth] = useState<string>("检查中…");
  const [sessionCount, setSessionCount] = useState<number | null>(null);
  const [taskCount, setTaskCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8030"}/api/health`)
      .then((r) => (cancelled ? null : setHealth(r.ok ? "正常" : "异常")))
      .catch(() => {
        if (!cancelled) setHealth("无法连接");
      });
    listSessions()
      .then((s) => {
        if (!cancelled) setSessionCount(s.length);
      })
      .catch(() => {});
    listTasks()
      .then((t) => {
        if (!cancelled) setTaskCount(t.length);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <h2 className="text-base font-semibold text-foreground mb-3">沙盒 / 状态</h2>
        <p className="text-sm text-muted">项目运行状态与统计（调试用）。</p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="bg-surface-2 rounded-xl p-4">
          <div className="text-xs text-muted mb-1">后端健康</div>
          <div className="text-lg font-semibold text-foreground">{health}</div>
        </div>
        <div className="bg-surface-2 rounded-xl p-4">
          <div className="text-xs text-muted mb-1">我的作品</div>
          <div className="text-lg font-semibold text-foreground">
            {sessionCount === null ? "…" : sessionCount}
          </div>
        </div>
        <div className="bg-surface-2 rounded-xl p-4">
          <div className="text-xs text-muted mb-1">短管线任务</div>
          <div className="text-lg font-semibold text-foreground">
            {taskCount === null ? "…" : taskCount}
          </div>
        </div>
      </div>

      <section className="space-y-2">
        <h3 className="text-sm font-medium text-foreground">关于本项目</h3>
        <p className="text-sm text-muted leading-relaxed">
          AI造梦机 —— 从一句创意到一部完整成片的 AI 短片创作系统。6 阶段主流程 + 3
          条短管线，邀请码登录与账号隔离，内容安全提示词侧 + 结果侧双向拦截。
        </p>
      </section>
    </div>
  );
}
