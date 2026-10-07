import { api, authHeaders } from "./client";
import { API_BASE } from "../config";

export type AssetItem = { asset_id: string; source_type: "session" | "task"; source_id: string; source_title: string; stage: string; kind: "image"; name: string; url: string; updated_at: number; stale: boolean; version_id: string | null };
export function listAssets(): Promise<AssetItem[]> { return api<AssetItem[]>("/api/assets"); }
export function reuseAsset(assetId: string): Promise<{ filename: string; original_name: string; asset: AssetItem }> { return api("/api/assets/reuse", { method: "POST", body: JSON.stringify({ asset_id: assetId }) }); }
export async function loadAssetImage(assetId: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/assets/${encodeURIComponent(assetId)}/media`, { headers: authHeaders() });
  if (!res.ok) throw new Error("素材已不可用，请重新选择");
  return URL.createObjectURL(await res.blob());
}
