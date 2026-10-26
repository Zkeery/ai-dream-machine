import { api } from "@/lib/api/client";
import type { SessionMeta } from "@/lib/api/sessions";

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, ms);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

// Only the public showcase retries. Each attempt is bounded, and leaving the
// page cancels both pending requests and the retry delay.
export async function loadDemoSessions(signal: AbortSignal): Promise<SessionMeta[]> {
  for (let attempt = 0; attempt < 3; attempt++) {
    signal.throwIfAborted();
    const request = new AbortController();
    const abort = () => request.abort(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(() => request.abort(), 8000);
    try {
      return await api<SessionMeta[]>("/api/sessions", { signal: request.signal, cache: "no-store" });
    } catch (error) {
      if (signal.aborted || attempt === 2) throw error;
    } finally {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
    }
    await pause(500 * (attempt + 1), signal);
  }
  throw new Error("Example loading exhausted");
}
