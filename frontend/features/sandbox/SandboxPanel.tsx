"use client";

import { useEffect, useState } from "react";
import { getSandboxStatus } from "@/lib/api/admin";

export function SandboxPanel() {
  const [health, setHealth] = useState<string>("检查中…");
  const [sessionCount, setSessionCount] = useState<number | null>(null);
  const [taskCount, setTaskCount] = useState<number | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    getSandboxStatus().then(status => {
      if (cancelled) return;
      setHealth(status.status === "ok" ? "正常" : "异常");
      setSessionCount(status.session_count);
      setTaskCount(status.task_count);
    }).catch(e => {
      if (cancelled) return;
      setHealth("无法读取");
      setError(e instanceof Error ? e.message : "状态读取失败，请重试");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="max-w-2xl space-y-6">
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
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
          AI造梦机采用故事短视频、漫剧两条独立创作链路，并提供3种短视频快捷工具。
          此页面仅供管理员检查服务状态，数量为当前账号的数据。
        </p>
      </section>
    </div>
  );
}
