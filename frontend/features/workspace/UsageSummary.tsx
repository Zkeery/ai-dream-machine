"use client";

import { useEffect, useState } from "react";
import { cnyLabel, getAccountUsage, type AccountUsage } from "@/lib/api/usage";

export function UsageSummary() {
  const [usage, setUsage] = useState<AccountUsage | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try { const value = await getAccountUsage(); if (active) { setUsage(value); setError(""); } }
      catch (e) { if (active) setError(e instanceof Error ? e.message : "额度读取失败"); }
      if (active) timer = setTimeout(refresh, 10000);
    };
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, [retry]);

  return <aside className="usage-strip" aria-label="本月生成额度">
    {!usage ? <span className="usage-note">{error || "正在读取本月生成额度…"}</span> : <><span className="usage-title">{usage.month} · 本月生成额度</span><dl><div><dt>按表估算</dt><dd>{cnyLabel(usage.calculated_cny)}</dd></div><div><dt>已预留</dt><dd>{cnyLabel(usage.reserved_cny)}</dd></div><div><dt>可用额度</dt><dd>{cnyLabel(usage.remaining_cny)}</dd></div></dl><span className="usage-concurrency">运行中 {usage.concurrency.active} / {usage.concurrency.account_limit}</span></>}
    {error && <span className="usage-error" role="status">{usage ? `额度暂未更新：${error}` : ""}<button className="text-link" onClick={() => setRetry(value => value + 1)}>重试</button></span>}
  </aside>;
}
