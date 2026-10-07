"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { getRecovery, recoveryExecutionId, recoveryNeedsCheck, type RecoveryKind, type RecoveryStatus } from "@/lib/api/recovery";

export function RecoveryNotice({ kind, entityId, status, revision, busy, onResume }: {
  kind: RecoveryKind; entityId: string; status: string; revision?: number; busy: boolean;
  onResume: (executionId: string) => Promise<void>;
}) {
  const [result, setResult] = useState<RecoveryStatus | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [resuming, setResuming] = useState(false);
  const inFlight = useRef(false);
  const eligible = status === "failed" || status === "interrupted";

  useEffect(() => {
    if (!eligible) return;
    let active = true;
    getRecovery(kind, entityId).then(value => {
      if (active) { setResult(value); setError(""); setLoading(false); }
    }).catch(e => { if (active) { setError(e instanceof Error ? e.message : "恢复状态读取失败"); setLoading(false); } });
    return () => { active = false; };
  }, [kind, entityId, eligible, revision, retry]);

  async function resume() {
    const executionId = result ? recoveryExecutionId(result) : null;
    if (!eligible || busy || loading || error || !executionId || inFlight.current) return;
    inFlight.current = true; setResuming(true); setError("");
    try { await onResume(executionId); }
    catch (e) { setError(e instanceof Error ? e.message : "继续失败，请刷新恢复状态后重试"); }
    finally { inFlight.current = false; setResuming(false); }
  }

  if (!eligible) return null;
  const check = result && recoveryNeedsCheck(result);
  const executionId = result && recoveryExecutionId(result);
  const invalidOutput = result?.reason_code === "AGENT_OUTPUT_INVALID";
  return <section className="recovery-notice" aria-label="未完成生成的恢复">
    <div className="recovery-heading"><strong>{invalidOutput ? "Agent 回复格式错误" : check ? "模型侧结果待核对" : executionId ? "继续这次生成" : "生成未完成"}</strong><button className="text-link" disabled={busy || resuming || loading} onClick={() => { setLoading(true); setResult(null); setError(""); setRetry(value => value + 1); }}>刷新恢复状态</button></div>
    {loading ? <p role="status">正在查询已保存的模型任务…</p> : error ? <p className="text-danger" role="alert">{error}</p> : check ? <p>有模型提交结果尚未确认，暂不能安全继续。请先核对模型侧任务与费用，再刷新状态；直接重新生成可能产生另一笔费用。</p> : executionId ? <><p>沿用这次生成的原始输入和模型。已提交的模型任务只查询进度或下载结果；尚未提交的部分可能正常计费。</p><Button variant="secondary" disabled={busy || resuming} onClick={() => void resume()}>{resuming ? "正在继续…" : kind === "session" ? "继续未完成阶段" : "继续未完成任务"}</Button></> : <p>{result?.reason || (result?.jobs.length ? "当前任务暂不满足续接条件。" : "没有可续接的模型任务记录。")}可检查错误后重新生成；重新生成会提交新的生成请求，可能产生费用。</p>}
    {!loading && !error && Boolean(result?.jobs.length) && <details><summary>已记录的模型任务（{result?.jobs.length}）</summary><ul>{result?.jobs.map((job, index) => <li key={job.id ?? index}><span>{job.model}</span><span>{job.requires_reconciliation ? "结果待核对" : job.status === "downloaded" ? "结果已下载" : job.status === "completed" ? "结果已生成" : job.status === "failed" ? "模型任务失败" : job.resumable ? "可查询或下载" : "待核对"}</span></li>)}</ul></details>}
  </section>;
}
