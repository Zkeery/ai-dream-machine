"use client";

import { useEffect, useState } from "react";
import { getSettings, type Settings } from "@/lib/api/settings";

const MODEL_LABELS: Record<string, string> = {
  llm: "文本（剧本/分镜）",
  vlm: "视觉（内容审查）",
  image_t2i: "文生图",
  video_first_frame: "视频（首帧）",
  video_reference: "视频（参考图）",
};

export function SettingsPanel() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    getSettings()
      .then((s) => {
        if (!cancelled) setSettings(s);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "加载失败");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <p className="text-sm text-danger">{error}</p>;
  if (!settings) return <p className="text-sm text-muted">加载中…</p>;

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <h2 className="text-base font-semibold text-foreground mb-3">设置</h2>
        <p className="text-sm text-muted">
          当前模型配置与安全开关（只读；如需修改，请调整后端 .env 后重启）。
        </p>
      </div>

      <section className="space-y-2">
        <h3 className="text-sm font-medium text-foreground">模型网关</h3>
        <div className="bg-surface-2 rounded-lg px-3 py-2 text-sm text-foreground">
          {settings.gateway}
        </div>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-medium text-foreground">模型</h3>
        <ul className="space-y-2">
          {Object.entries(settings.models).map(([key, value]) => (
            <li key={key} className="flex items-center justify-between text-sm border border-border rounded-lg px-3 py-2">
              <span className="text-muted">{MODEL_LABELS[key] ?? key}</span>
              <span className="text-foreground font-mono">{value}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-medium text-foreground">内容安全</h3>
        <div className="flex items-center gap-2 text-sm">
          <span
            className={`inline-block w-2.5 h-2.5 rounded-full ${
              settings.content_review_enabled ? "bg-success" : "bg-muted"
            }`}
          />
          <span className="text-foreground">
            结果侧审查（VLM）：{settings.content_review_enabled ? "已开启" : "已关闭"}
          </span>
        </div>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-medium text-foreground">登录令牌有效期</h3>
        <p className="text-sm text-foreground">{settings.token_ttl_days} 天</p>
      </section>
    </div>
  );
}
