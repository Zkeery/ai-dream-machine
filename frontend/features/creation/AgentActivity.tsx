import type { AgentRun } from "@/lib/api/sessions";

const roles: Record<string, string> = { planner: "规划", researcher: "资料研究", writer: "编剧", director: "分镜导演", designer: "视觉设计", producer: "制作", reviewer: "审稿" };
const statuses: Record<string, string> = { running: "协作中", completed: "已完成", failed: "执行中断", needs_review: "请人工检查" };

export function AgentActivity({ runs }: { runs: AgentRun[] }) {
  const latest = runs.at(-1);
  return <section className="rounded-xl border border-border p-4 space-y-3" aria-label="Agent 协作记录">
    <div className="flex justify-between gap-3"><strong className="text-sm">多 Agent 自主协作</strong><span className="text-xs text-muted">{latest ? statuses[latest.status] ?? latest.status : "等待开始"}</span></div>
    <p className="text-xs text-muted">规划、专业执行与独立审稿；审稿基于文本及元数据，画面和声音由你检查。</p>
    {[...runs].reverse().map((run, i) => <details key={run.execution_id} open={i === 0} className="text-sm space-y-2">
      <summary className="cursor-pointer">{i === 0 ? "本阶段最近一次协作" : "历史协作"} · {new Date(run.started_at * 1000).toLocaleString("zh-CN")} · {run.decisions} 次决策</summary>
      {run.plan && <><p>{run.plan.summary}</p><ul className="space-y-1">{run.plan.tasks.map(task => <li key={task.id}><strong>{roles[task.role] ?? task.role}</strong> · {task.objective} <span className="text-muted">（{statuses[run.tasks[task.id]?.status] ?? "等待执行"}）</span></li>)}</ul></>}
      <ol className="space-y-2 text-xs text-muted">{run.events.map((event, j) => <li key={j}><strong>{roles[event.role] ?? event.role}</strong> · {event.summary}</li>)}</ol>
      {run.reviews.at(-1) && <p className="text-sm">审稿结论：{run.reviews.at(-1)?.summary}</p>}
    </details>)}
  </section>;
}
