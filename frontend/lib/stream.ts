import { ApiError, authHeaders } from "./api/client";
import { API_BASE } from "./config";

export type SSEEvent =
  | { type: "progress"; stage: string; message: string; percent: number }
  | { type: "done"; session: Record<string, unknown> }
  | { type: "error"; error: { code: string; message: string } };

/** 用 Fetch Streaming 消费后端 SSE（支持 Bearer 头）。 */
export async function streamSSE(
  url: string,
  options: RequestInit,
  onEvent: (ev: SSEEvent) => void,
): Promise<void> {
  const headers = new Headers(options.headers);
  for (const [k, v] of Object.entries(authHeaders())) headers.set(k, v);
  if (options.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${API_BASE}${url}`, { ...options, headers });

  if (!res.ok || !res.headers.get("content-type")?.includes("text/event-stream")) {
    let message = "请求失败";
    let code = "STREAM_ERROR";
    try {
      const body = await res.json();
      if (body?.error) {
        code = body.error.code || code;
        message = body.error.message || message;
      }
    } catch {
      // 非 JSON
    }
    throw new ApiError(code, message, res.status);
  }

  if (!res.body) throw new ApiError("STREAM_ERROR", "无法读取响应流", 500);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      try {
        onEvent(JSON.parse(line.slice(6)) as SSEEvent);
      } catch {
        // 忽略无法解析的分片
      }
    }
  }
}
