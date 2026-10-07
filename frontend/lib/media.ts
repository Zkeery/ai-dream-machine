import { authHeaders } from "./api/client";
import { API_BASE } from "./config";

/** 从产物绝对路径提取 kind 与 filename。 */
export function mediaInfo(path: string): { kind: string; filename: string } {
  const parts = path.split("/");
  const filename = parts[parts.length - 1] || "";
  const kind = path.includes("/video/") ? "video" : path.includes("/image/") ? "image" : "script";
  return { kind, filename };
}

/** 带鉴权加载媒体为 blob ObjectURL；用完应 revoke。 */
export async function loadMediaBlob(
  sessionId: string,
  path: string,
): Promise<string> {
  const { kind, filename } = mediaInfo(path);
  const res = await fetch(`${API_BASE}/api/sessions/${sessionId}/media/${kind}/${filename}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("媒体加载失败");
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

/** 下载成片（带鉴权，blob → 触发保存）。 */
export async function downloadMedia(sessionId: string, filename = "成片.mp4", versionId?: string): Promise<void> {
  const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : "";
  const res = await fetch(`${API_BASE}/api/sessions/${sessionId}/export${query}`, { headers: authHeaders() });
  if (!res.ok) throw new Error("下载失败");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
