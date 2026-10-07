import { api, authHeaders, ApiError } from "./client";
import { streamSSE, type SSEEvent } from "../stream";
import { API_BASE } from "../config";
import type { ModelUsage } from "./models";

export type TaskMeta = {
  task_id: string;
  type: string;
  owner_id: string;
  status: string;
  input: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: number;
  updated_at: number;
  execution?: { execution_id?: string; status: string; last_event?: { message?: string; percent?: number } | null; error?: string | null; model_usage?: ModelUsage };
};

export function createTask(
  type: string,
  input: Record<string, unknown>,
  idempotencyKey?: string,
): Promise<{ task_id: string }> {
  return api<{ task_id: string }>("/api/tasks", {
    method: "POST",
    headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
    body: JSON.stringify({ type, input }),
  });
}

export function listTasks(): Promise<TaskMeta[]> {
  return api<TaskMeta[]>("/api/tasks");
}

export function getTask(taskId: string): Promise<TaskMeta> {
  return api<TaskMeta>(`/api/tasks/${taskId}`);
}

export async function loadTaskVideo(taskId: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/tasks/${taskId}/export`, { headers: authHeaders() });
  if (!res.ok) throw new Error("成片预览加载失败，请重试");
  return URL.createObjectURL(await res.blob());
}

export function streamTask(taskId: string, onEvent: (ev: SSEEvent) => void, signal?: AbortSignal): Promise<void> {
  return streamSSE(`/api/tasks/${taskId}/stream`, { method: "GET", signal }, onEvent);
}

export async function uploadFile(file: File): Promise<string> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${API_BASE}/api/upload`, { method: "POST", headers: authHeaders(), body: fd });
  if (!res.ok) {
    let message = "上传失败";
    try {
      const body = await res.json();
      message = body?.error?.message || message;
    } catch {
      // ignore
    }
    throw new ApiError("UPLOAD_FAILED", message, res.status);
  }
  const data = (await res.json()) as { file_path: string };
  return data.file_path.split("/").pop() ?? "";
}

export async function downloadTask(taskId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/tasks/${taskId}/export`, { headers: authHeaders() });
  if (!res.ok) throw new Error("下载失败");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "成片.mp4";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
