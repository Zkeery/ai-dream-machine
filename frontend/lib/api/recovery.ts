import { api } from "./client";
import { streamSSE, type SSEEvent } from "../stream";

export type RecoveryStatus = {
  execution_id: string | null;
  source_execution_id: string | null;
  can_resume: boolean;
  requires_reconciliation: boolean;
  reason?: string;
  reason_code?: string;
  jobs: { id?: string; kind: string; model: string; status: string; resumable: boolean; requires_reconciliation?: boolean }[];
};
export type RecoveryKind = "session" | "task";

export function getRecovery(kind: RecoveryKind, id: string): Promise<RecoveryStatus> {
  return api(`/api/${kind === "session" ? "sessions" : "tasks"}/${encodeURIComponent(id)}/recovery`);
}

export function resumeSession(id: string, executionId: string, onEvent: (event: SSEEvent) => void, signal?: AbortSignal): Promise<void> {
  return streamSSE(`/api/sessions/${encodeURIComponent(id)}/resume`, {
    method: "POST", body: JSON.stringify({ execution_id: executionId }), signal,
  }, onEvent);
}

export function resumeTask(id: string, executionId: string): Promise<{ task_id: string }> {
  return api(`/api/tasks/${encodeURIComponent(id)}/resume`, {
    method: "POST", body: JSON.stringify({ execution_id: executionId }),
  });
}

export function recoveryNeedsCheck(status: RecoveryStatus): boolean {
  return status.requires_reconciliation || status.jobs.some(job => job.requires_reconciliation || ((job.status === "unknown" || job.status === "submitting") && !job.resumable));
}

export function recoveryExecutionId(status: RecoveryStatus): string | null {
  return status.can_resume && !recoveryNeedsCheck(status) ? status.source_execution_id || status.execution_id : null;
}
