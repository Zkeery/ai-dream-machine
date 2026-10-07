"use client";

import { useEffect, useState } from "react";
import { BookOpen, RefreshCw } from "lucide-react";
import { listKnowledgeLibraries, type KnowledgeLibrary } from "@/lib/api/knowledge";
import { toggleKnowledgeLibrary, unavailableKnowledgeIds } from "@/lib/knowledge";

export function KnowledgeLibraryPicker({ selected, onChange, disabled = false, onManage }: { selected: string[]; onChange: (ids: string[]) => void; disabled?: boolean; onManage?: () => void }) {
  const [libraries, setLibraries] = useState<KnowledgeLibrary[] | null>(null);
  const [error, setError] = useState("");
  async function reload() { try { setLibraries(await listKnowledgeLibraries()); setError(""); } catch (e) { setError(e instanceof Error ? e.message : "知识库加载失败"); } }
  useEffect(() => { let cancelled = false; listKnowledgeLibraries().then(value => { if (!cancelled) setLibraries(value); }).catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : "知识库加载失败"); }); return () => { cancelled = true; }; }, []);
  const unavailable = libraries ? unavailableKnowledgeIds(selected, libraries) : [];
  function toggle(id: string, checked: boolean) { try { onChange(toggleKnowledgeLibrary(selected, id, checked)); setError(""); } catch (e) { setError(e instanceof Error ? e.message : "无法选择知识库"); } }
  return <details className="knowledge-picker"><summary><BookOpen size={13} />创作资料<span>{selected.length ? `已选 ${selected.length} 个知识库` : "可选"}</span></summary><div className="knowledge-picker-body">
    <div className="knowledge-inline-heading"><span>最多 3 个知识库 · 剧本与分镜生成时检索</span><button type="button" className="text-link" onClick={() => void reload()} aria-label="刷新知识库"><RefreshCw size={12} /></button></div>
    {error && <p className="knowledge-error" role="alert">{error}</p>}
    {libraries === null && !error ? <p>加载知识库…</p> : libraries && !libraries.length ? <p>还没有知识库，先整理世界观、角色或品牌资料。</p> : libraries?.map(library => <label key={library.library_id}><input type="checkbox" disabled={disabled} checked={selected.includes(library.library_id)} onChange={e => toggle(library.library_id, e.target.checked)} /><span><strong>{library.name}</strong><small>{library.document_count} 份资料{library.description ? ` · ${library.description}` : ""}</small></span></label>)}
    {unavailable.map(id => <div key={id} className="knowledge-unavailable">已选知识库不可用<button type="button" disabled={disabled} onClick={() => toggle(id, false)}>移除</button></div>)}
    {onManage && <button type="button" className="text-link" onClick={onManage}>管理创作知识库 ↗</button>}
  </div></details>;
}
