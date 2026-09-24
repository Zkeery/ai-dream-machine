"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { createSession, type SessionMeta } from "@/lib/api/sessions";

const inputCls =
  "w-full px-3 py-2 rounded-md bg-surface-2 border border-border text-foreground text-sm placeholder:text-muted focus:outline-none focus:border-primary";

export function CreationForm({
  onCreated,
}: {
  onCreated: (s: SessionMeta) => void;
}) {
  const [idea, setIdea] = useState("");
  const [style, setStyle] = useState("realistic");
  const [episodes, setEpisodes] = useState(4);
  const [ratio, setRatio] = useState("16:9");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!idea.trim()) {
      setError("请输入创意");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const s = await createSession({
        idea: idea.trim(),
        style,
        episodes,
        video_ratio: ratio,
      });
      onCreated(s);
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 max-w-xl">
      <div>
        <h2 className="text-base font-semibold text-foreground mb-3">开始创作</h2>
        <label htmlFor="idea" className="block text-sm text-muted mb-1">
          创作创意
        </label>
        <textarea
          id="idea"
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          placeholder="例：一只流浪猫在雨夜被好心人收留"
          rows={4}
          autoFocus
          className={`${inputCls} resize-y`}
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div>
          <label htmlFor="style" className="block text-sm text-muted mb-1">
            视觉风格
          </label>
          <Input
            id="style"
            value={style}
            onChange={(e) => setStyle(e.target.value)}
            placeholder="realistic / anime"
          />
        </div>
        <div>
          <label htmlFor="episodes" className="block text-sm text-muted mb-1">
            剧集数
          </label>
          <Input
            id="episodes"
            type="number"
            min={1}
            max={20}
            value={episodes}
            onChange={(e) => setEpisodes(Number(e.target.value))}
          />
        </div>
        <div>
          <label htmlFor="ratio" className="block text-sm text-muted mb-1">
            视频比例
          </label>
          <select
            id="ratio"
            value={ratio}
            onChange={(e) => setRatio(e.target.value)}
            className={inputCls}
          >
            <option value="16:9">16:9</option>
            <option value="9:16">9:16</option>
            <option value="1:1">1:1</option>
          </select>
        </div>
      </div>

      {error && <p className="text-sm text-danger">{error}</p>}

      <Button type="submit" disabled={submitting}>
        {submitting ? "创建中…" : "开始创作"}
      </Button>
    </form>
  );
}
