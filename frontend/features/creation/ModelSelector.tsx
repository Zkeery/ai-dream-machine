"use client";

import { ChevronDown, Cpu } from "lucide-react";
import type { ModelCatalog, ModelKey, ModelSelection } from "@/lib/api/models";
import { modelSelectionError, resolvedModelSelection } from "@/lib/workflow";

const LABELS: Record<ModelKey, string> = { text: "文本", image: "图片", video_first_frame: "首帧视频", video_start_end: "首尾帧视频", video_reference: "参考图视频", video_speech: "嘴型同步视频" };

export function ModelSelector({ catalog, loading, error, keys, selected, disabled = false, compact = false, onChange, onRetry }: {
  catalog: ModelCatalog | null; loading: boolean; error: string; keys: ModelKey[]; selected: ModelSelection;
  disabled?: boolean; compact?: boolean; onChange: (value: ModelSelection) => void; onRetry: () => void;
}) {
  if (!keys.length) return null;
  const value = resolvedModelSelection(selected, catalog, keys);
  const invalid = catalog ? modelSelectionError(value, catalog, keys) : "";
  const summary = loading ? "正在读取可用模型…" : error || !catalog ? "模型目录加载失败" : keys.map(key => {
    const group = catalog.groups[key] ?? { label: LABELS[key], default: null, options: [] };
    return `${LABELS[key]} ${!group.options.length ? "暂未开放" : group.options.find(option => option.id === value[key])?.label ?? (value[key] ? `${value[key]}（不可用）` : "待选择")}`;
  }).join(" · ");
  const fields = <div className="model-picker-fields">
    {catalog && keys.map(key => {
      const group = catalog.groups[key] ?? { label: LABELS[key], default: null, options: [] }, option = group.options.find(option => option.id === value[key]);
      return <label className="model-picker-field" key={key}><span>{group.label}</span>
        <select aria-label={`${LABELS[key]}模型`} disabled={disabled || loading || Boolean(error) || !group.options.length} value={value[key] ?? ""} onChange={e => onChange({ ...value, [key]: e.target.value })}>
          {!option && <option value={value[key] ?? ""}>{!group.options.length ? "暂未开放" : value[key] ? `${value[key]}（不可用，请重选）` : "请选择模型"}</option>}
          {group.options.map(item => <option key={item.id} value={item.id}>{item.label}{item.id === group.default ? " · 推荐" : ""}</option>)}
        </select>
        {option?.video && <small>{option.video.duration_seconds}秒/片段 · {Object.entries(option.video.estimated_clip_cny).map(([resolution, cost]) => `${resolution}约¥${cost.toFixed(2)}/片段`).join("；") || "暂无费用估算"}</small>}
      </label>;
    })}
  </div>;
  return <section className={`model-picker${compact ? " model-picker-compact" : ""}`} aria-label="生成模型设置">
    {compact ? <details onToggle={event => {
      const details = event.currentTarget;
      if (details.open && details.getBoundingClientRect().bottom > window.innerHeight) {
        details.scrollIntoView({ block: "nearest", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
      }
    }}><summary><Cpu size={14} aria-hidden="true" /><strong>模型</strong><span title={summary}>{summary}</span><ChevronDown size={14} aria-hidden="true" /></summary><div className="model-picker-popover">{fields}</div></details> : <><div className="model-picker-heading"><Cpu size={14} aria-hidden="true" /><strong>生成模型</strong></div>{loading ? <p role="status">{summary}</p> : fields}</>}
    {(error || invalid) && <p className="model-picker-error" role="alert">{error || invalid}{error && <button type="button" disabled={disabled || loading} onClick={onRetry}>重新加载模型</button>}</p>}
  </section>;
}
