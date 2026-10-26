"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { recheckContent, type CreationCheck, type SessionMeta } from "@/lib/api/sessions";

const LABELS = {
  review: "发现需要你核对的地方",
  evidence: "部分表述找到了资料依据",
  limited: "本次未发现规则能识别的问题",
  unavailable: "这次检查未完成，内容已保留",
};

function Findings({ report }: { report: CreationCheck }) {
  return <div className="space-y-3">
    <p className={report.status === "review" ? "text-danger" : "text-foreground"}>{LABELS[report.status]}</p>
    {report.findings.map((finding, index) => <article key={index} className="rounded-lg border border-border p-3 space-y-2">
      <p className="font-medium">{finding.location}</p>
      <blockquote className="text-muted break-words">{finding.excerpt}</blockquote>
      <p>{finding.problem}</p><p><strong>建议怎么改：</strong>{finding.suggestion}</p>
    </article>)}
  </div>;
}

export function CreationChecks({ session, stage, busy, onUpdated }: {
  session: SessionMeta; stage: string; busy: boolean; onUpdated: (value: SessionMeta) => void;
}) {
  const [checking, setChecking] = useState(false), [error, setError] = useState("");
  if (!["script_generation", "storyboard", "comic_storyboard"].includes(stage) || !session.artifacts[stage]) return null;
  const reports = (session.creation_checks ?? []).filter(report => report.stage === stage);
  const current = [...reports].reverse().find(report => report.matches_current);
  const versions = session.artifact_versions?.[stage] ?? [];
  async function check() {
    setChecking(true); setError("");
    try { onUpdated(await recheckContent(session.session_id, stage)); }
    catch (e) { setError(e instanceof Error ? e.message : "检查暂时不可用，请稍后重试"); }
    finally { setChecking(false); }
  }
  return <section aria-label="内容检查" className="rounded-xl border border-border bg-surface-2 p-4 space-y-3 text-sm">
    <div className="flex items-center justify-between gap-3"><h3 className="font-semibold">内容检查</h3>
      <Button variant="secondary" disabled={busy || checking} onClick={() => void check()}>{checking ? "正在检查…" : "重新检查"}</Button></div>
    <p className="text-xs text-muted">离线演示 · 自动核对部分因果、条件和保证表述。检查提示供你修改参考，不能代替完整人工验收，也未检查视频画面或声音。</p>
    {current ? <Findings report={current} /> : <p>当前内容还没有检查记录，点击“重新检查”即可核对。</p>}
    {error && <p role="alert" className="text-danger">{error}</p>}
    {reports.length > 0 && <details><summary>查看检查历史（{reports.length} 次）</summary>
      <p className="my-2 text-xs text-muted">历史检查随内容保留。使用上方“阶段版本”可恢复已生成版本；保存的修改不会覆盖旧版本。</p>
      <div className="space-y-3">{[...reports].reverse().map(report => {
        const index = versions.findIndex(version => version.version_id === report.version_id);
        return <details key={report.report_id} className="border-t border-border pt-2"><summary>
          {new Date(report.created_at * 1000).toLocaleString("zh-CN")} · {index >= 0 ? `版本 ${index + 1}` : "保存的修改"} · {report.matches_current ? "与当前内容一致" : "历史内容"}
        </summary><Findings report={report} /></details>;
      })}</div>
    </details>}
  </section>;
}
