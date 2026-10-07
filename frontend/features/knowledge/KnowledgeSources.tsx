"use client";

import { BookOpen } from "lucide-react";
import type { KnowledgeContext, KnowledgeSearch } from "@/lib/api/knowledge";

function statusLabel(status?: string) {
  if (status === "insufficient") return "资料未覆盖所问事实";
  if (status === "no_match") return "未找到相关资料";
  if (status === "matched") return "已命中相关资料";
  return status || "";
}

export function KnowledgeSources({ context, title = "搜索结果" }: { context: KnowledgeContext | KnowledgeSearch; title?: string }) {
  const rejected = context.rejected_sources ?? [];
  const warnings = context.warnings ?? [];
  return <details className="knowledge-sources" open><summary><BookOpen size={14} /><span>{title}</span>{context.status ? <em className="knowledge-status-tag">{statusLabel(context.status)}</em> : null}</summary><div className="knowledge-sources-body">
    {context.adequacy?.reason && <p className="knowledge-warning" role="status">{context.adequacy.degraded ? "充分性判定降级：" : "充分性判定："}{context.adequacy.reason}</p>}
    {warnings.map((warning, index) => <p className="knowledge-warning" key={`w-${index}`} role="status">{warning}</p>)}
    {(context.sources ?? []).map((source, i) => <article key={`${source.chunk_id}:${source.version_id}:${i}`} className="knowledge-source-card"><strong>{source.title}</strong><blockquote>{source.text}</blockquote></article>)}
    {!(context.sources ?? []).length && !rejected.length && <p className="knowledge-muted">未找到相关资料</p>}
    {!!rejected.length && <div className="knowledge-rejected"><p className="knowledge-muted">以下资料已检索到，但不足以支持所问事实，不会作为生成依据：</p>{rejected.map((source, i) => <article key={`r-${source.chunk_id}:${source.version_id}:${i}`} className="knowledge-source-card knowledge-source-rejected"><strong>{source.title}</strong><blockquote>{source.text}</blockquote></article>)}</div>}
  </div></details>;
}
