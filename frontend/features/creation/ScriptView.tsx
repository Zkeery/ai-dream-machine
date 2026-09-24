"use client";

import type { ScriptArtifact } from "@/lib/api/sessions";

export function ScriptView({ script }: { script: ScriptArtifact }) {
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-semibold text-foreground">{script.title || "未命名"}</h2>
        {script.logline && <p className="text-sm text-muted mt-1">{script.logline}</p>}
        {(script.genre?.length > 0 || script.mood) && (
          <p className="text-xs text-muted mt-2">
            {(script.genre || []).join(" / ")}
            {script.mood ? ` · ${script.mood}` : ""}
          </p>
        )}
      </div>

      {script.characters?.length > 0 && (
        <section>
          <h3 className="text-sm font-medium text-foreground mb-2">角色</h3>
          <ul className="space-y-2">
            {script.characters.map((c, i) => (
              <li key={c.character_id || i} className="text-sm">
                <span className="text-foreground font-medium">{c.name}</span>
                {c.role && <span className="text-accent">（{c.role}）</span>}
                {c.description && <span className="text-muted">：{c.description}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {script.settings?.length > 0 && (
        <section>
          <h3 className="text-sm font-medium text-foreground mb-2">场景</h3>
          <ul className="space-y-1">
            {script.settings.map((s, i) => (
              <li key={s.setting_id || i} className="text-sm">
                <span className="text-foreground">{s.name}</span>
                {s.description && <span className="text-muted">：{s.description}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {script.episodes?.length > 0 && (
        <section>
          <h3 className="text-sm font-medium text-foreground mb-2">剧集</h3>
          <div className="space-y-4">
            {script.episodes.map((ep) => (
              <div key={ep.episode_number}>
                <div className="text-sm font-medium text-foreground">
                  第 {ep.episode_number} 集{ep.act_title ? ` ${ep.act_title}` : ""}
                </div>
                <p className="text-sm text-muted whitespace-pre-wrap mt-1">{ep.content}</p>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
