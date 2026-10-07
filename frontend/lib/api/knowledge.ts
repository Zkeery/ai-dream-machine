import { api, ApiError, authHeaders } from "./client";
import { API_BASE } from "../config";
import { knowledgeUploadForm } from "../knowledge";

export type KnowledgeCategory = "world" | "character" | "plot" | "style" | "brand" | "other";
export const KNOWLEDGE_CATEGORIES: Record<KnowledgeCategory, string> = { world: "世界观", character: "角色设定", plot: "剧情资料", style: "视觉风格", brand: "品牌规则", other: "其他资料" };
export type KnowledgeLibrary = { library_id: string; name: string; description: string; updated_at: number; document_count: number; revision?: number };
export type KnowledgeDocument = { document_id: string; library_id: string; title: string; category: KnowledgeCategory; is_constraint: boolean; version_id: string; version_number: number; chunk_count: number; created_at: number; updated_at: number };
export type KnowledgeVersion = { version_id: string; version_number: number; created_at: number; chunk_count: number };
export type KnowledgeDetail = KnowledgeDocument & { text: string; versions: KnowledgeVersion[] };
export type KnowledgeSource = { citation_id: string; document_id: string; version_id: string; title: string; category: KnowledgeCategory; text: string; score: number | null; chunk_id: string; is_constraint: boolean };
export type KnowledgeAdequacy = { sufficient: boolean; reason?: string; unsupported_facts?: string[]; degraded?: boolean; error_code?: string; latency_ms?: number; model?: string };
export type KnowledgeSearch = { query: string; sources: KnowledgeSource[]; rejected_sources?: KnowledgeSource[]; warnings: string[]; status: "matched" | "no_match" | "insufficient"; adequacy?: KnowledgeAdequacy | null; retrieval_mode?: string; adequacy_enabled?: boolean };
export type KnowledgeContext = KnowledgeSearch & { library_ids: string[]; library_versions?: Record<string, unknown>; embedding_model?: string; [key: string]: unknown };

export function listKnowledgeLibraries(): Promise<KnowledgeLibrary[]> { return api("/api/knowledge/libraries"); }
export function createKnowledgeLibrary(name: string, description: string): Promise<KnowledgeLibrary> { return api("/api/knowledge/libraries", { method: "POST", body: JSON.stringify({ name, description }) }); }
export function updateKnowledgeLibrary(id: string, name: string, description: string): Promise<KnowledgeLibrary> { return api(`/api/knowledge/libraries/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ name, description }) }); }
export function deleteKnowledgeLibrary(id: string): Promise<unknown> { return api(`/api/knowledge/libraries/${encodeURIComponent(id)}`, { method: "DELETE" }); }
export function listKnowledgeDocuments(id: string): Promise<KnowledgeDocument[]> { return api(`/api/knowledge/libraries/${encodeURIComponent(id)}/documents`); }
export function getKnowledgeDocument(id: string): Promise<KnowledgeDetail> { return api(`/api/knowledge/documents/${encodeURIComponent(id)}`); }
export function getKnowledgeVersion(id: string, versionId: string): Promise<KnowledgeVersion & { text: string }> { return api(`/api/knowledge/documents/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}`); }
export function updateKnowledgeDocument(id: string, input: { title: string; category: KnowledgeCategory; is_constraint: boolean }): Promise<KnowledgeDocument> { return api(`/api/knowledge/documents/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(input) }); }
export function deleteKnowledgeDocument(id: string): Promise<unknown> { return api(`/api/knowledge/documents/${encodeURIComponent(id)}`, { method: "DELETE" }); }
export function searchKnowledgeLibrary(id: string, query: string): Promise<KnowledgeSearch> { return api(`/api/knowledge/libraries/${encodeURIComponent(id)}/search`, { method: "POST", body: JSON.stringify({ query, limit: 5 }) }); }

export async function uploadKnowledgeDocument(libraryId: string, input: { file: File; title: string; category: KnowledgeCategory; is_constraint: boolean }, documentId?: string): Promise<KnowledgeDocument> {
  const form = knowledgeUploadForm(input);
  const path = documentId ? `/api/knowledge/documents/${encodeURIComponent(documentId)}/versions` : `/api/knowledge/libraries/${encodeURIComponent(libraryId)}/documents`;
  const response = await fetch(`${API_BASE}${path}`, { method: "POST", headers: authHeaders(), body: form });
  if (!response.ok) {
    let code = "KNOWLEDGE_UPLOAD_FAILED", message = "资料上传失败，输入已保留，请重试";
    try { const body = await response.json(); code = body?.error?.code || code; message = body?.error?.message || message; } catch {}
    throw new ApiError(code, message, response.status);
  }
  return response.json();
}
