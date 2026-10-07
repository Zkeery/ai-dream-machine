"use client";

import { useCallback, useEffect, useState } from "react";
import { BookOpen, FileText, FolderPlus, Plus, Search, Upload } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { KNOWLEDGE_CATEGORIES, createKnowledgeLibrary, deleteKnowledgeDocument, deleteKnowledgeLibrary, getKnowledgeDocument, getKnowledgeVersion, listKnowledgeDocuments, listKnowledgeLibraries, searchKnowledgeLibrary, updateKnowledgeDocument, updateKnowledgeLibrary, uploadKnowledgeDocument, type KnowledgeCategory, type KnowledgeDetail, type KnowledgeDocument, type KnowledgeLibrary, type KnowledgeSearch } from "@/lib/api/knowledge";
import { validateKnowledgeFile } from "@/lib/knowledge";
import { KnowledgeSources } from "./KnowledgeSources";

function LibraryForm({ library, onSave, disabled }: { library?: KnowledgeLibrary; onSave: (name: string, description: string) => Promise<void>; disabled: boolean }) {
  const [name, setName] = useState(library?.name ?? "");
  const [description, setDescription] = useState(library?.description ?? "");
  return <form className="knowledge-form" onSubmit={e => { e.preventDefault(); if (name.trim()) void onSave(name.trim(), description.trim()); }}><label><span>知识库名称</span><Input value={name} maxLength={80} disabled={disabled} onChange={e => setName(e.target.value)} placeholder="例如：云海之城 · 世界观" required /></label><label><span>用途说明</span><textarea value={description} maxLength={1000} disabled={disabled} onChange={e => setDescription(e.target.value)} rows={2} placeholder="记录故事背景、人物关系与创作规则" /></label><Button disabled={disabled || !name.trim()} type="submit">{disabled ? "保存中…" : library ? "保存知识库信息" : "创建知识库"}</Button></form>;
}

function DocumentUpload({ libraryId, existing, disabled, onUploaded, onError }: { libraryId: string; existing?: KnowledgeDocument; disabled: boolean; onUploaded: (document: KnowledgeDocument) => Promise<void>; onError: (message: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState(existing?.title ?? "");
  const [category, setCategory] = useState<KnowledgeCategory>(existing?.category ?? "world");
  const [constraint, setConstraint] = useState(existing?.is_constraint ?? false);
  const [uploading, setUploading] = useState(false);
  const [inputKey, setInputKey] = useState(0);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return onError("请选择包含文字的资料文件");
    const invalid = validateKnowledgeFile(file);
    if (invalid) return onError(invalid);
    setUploading(true); onError("");
    try {
      const document = await uploadKnowledgeDocument(libraryId, { file, title: title.trim(), category, is_constraint: constraint }, existing?.document_id);
      await onUploaded(document); setFile(null); setInputKey(key => key + 1); if (!existing) setTitle("");
    } catch (e) { onError(e instanceof Error ? e.message : "上传失败，文件与填写内容已保留"); }
    finally { setUploading(false); }
  }
  const busy = disabled || uploading;
  return <form className="knowledge-form" onSubmit={e => void submit(e)}>
    <label className="knowledge-upload-zone"><Upload size={20} /><strong>{existing ? "上传替换文件，保存为新版本" : "选择创作资料"}</strong><span>Markdown / TXT / 可提取文字的 PDF · 单文件 ≤ 5 MB</span><input key={inputKey} type="file" accept=".md,.txt,.pdf,text/plain,text/markdown,application/pdf" disabled={busy} onChange={e => { const selected = e.target.files?.[0] ?? null; setFile(selected); if (selected) { const invalid = validateKnowledgeFile(selected); if (invalid) onError(invalid); else onError(""); } }} /></label>
    {file && <p className="knowledge-muted">已选：{file.name} · {(file.size / 1024).toFixed(1)} KB</p>}
    <div className="knowledge-form-grid"><label><span>资料标题</span><Input value={title} maxLength={160} disabled={busy} onChange={e => setTitle(e.target.value)} placeholder="留空时使用文件名" /></label><label><span>资料分类</span><select value={category} disabled={busy} onChange={e => setCategory(e.target.value as KnowledgeCategory)}>{Object.entries(KNOWLEDGE_CATEGORIES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div>
    <label className="knowledge-toggle"><input type="checkbox" checked={constraint} disabled={busy} onChange={e => setConstraint(e.target.checked)} /><span><strong>作为固定约束</strong><small>生成时总是提供这份规则。单库与所选多库的固定约束合计均不超过 6000 字符。</small></span></label>
    <p className="knowledge-muted">扫描版 PDF 需要先转成文字；每份资料提取后最多 20 万字符。{existing ? "历史版本继续保留，已生成故事不会自动重生成。" : "资料仅在当前账号的知识库中使用。"}</p>
    <Button disabled={busy || !file} type="submit">{uploading ? "上传并建立索引…" : existing ? "保存替换版本" : "上传资料"}</Button>
  </form>;
}

function DocumentDetail({ detail, onChange, onRefresh, disabled, onError }: { detail: KnowledgeDetail; onChange: (detail: KnowledgeDetail) => void; onRefresh: () => Promise<void>; disabled: boolean; onError: (message: string) => void }) {
  const [title, setTitle] = useState(detail.title);
  const [category, setCategory] = useState(detail.category);
  const [constraint, setConstraint] = useState(detail.is_constraint);
  const [text, setText] = useState(detail.text);
  const [versionId, setVersionId] = useState(detail.version_id);
  const [busy, setBusy] = useState(false);
  async function viewVersion(version: string) {
    setBusy(true); onError("");
    try { const value = await getKnowledgeVersion(detail.document_id, version); setText(value.text); setVersionId(version); }
    catch (e) { onError(e instanceof Error ? e.message : "版本原文加载失败"); }
    finally { setBusy(false); }
  }
  async function save(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); onError("");
    try { await updateKnowledgeDocument(detail.document_id, { title: title.trim(), category, is_constraint: constraint }); onChange(await getKnowledgeDocument(detail.document_id)); await onRefresh(); }
    catch (e) { onError(e instanceof Error ? e.message : "资料设置保存失败"); }
    finally { setBusy(false); }
  }
  return <div className="knowledge-document-detail">
    <div className="knowledge-detail-heading"><div><p className="section-kicker">SOURCE DOCUMENT</p><h3>{detail.title}</h3><p>{KNOWLEDGE_CATEGORIES[detail.category]} · 当前版本 {detail.version_number} · {detail.chunk_count} 个片段</p></div>{detail.is_constraint && <span className="knowledge-constraint">固定约束</span>}</div>
    <div className="knowledge-version-bar"><label htmlFor="knowledge-version">查看原文版本</label><select id="knowledge-version" value={versionId} disabled={busy || disabled} onChange={e => void viewVersion(e.target.value)}>{detail.versions.map(version => <option key={version.version_id} value={version.version_id}>版本 {version.version_number} · {new Date(version.created_at * 1000).toLocaleString("zh-CN")}</option>)}</select>{versionId !== detail.version_id && <span className="knowledge-constraint">历史原文</span>}</div>
    <pre className="knowledge-document-text">{text || "这份资料没有可展示的文字。"}</pre>
    <details className="knowledge-edit-details"><summary>修改标题、分类与固定约束</summary><form className="knowledge-form" onSubmit={e => void save(e)}><label><span>资料标题</span><Input value={title} maxLength={160} required disabled={busy || disabled} onChange={e => setTitle(e.target.value)} /></label><label><span>资料分类</span><select value={category} disabled={busy || disabled} onChange={e => setCategory(e.target.value as KnowledgeCategory)}>{Object.entries(KNOWLEDGE_CATEGORIES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="knowledge-toggle"><input type="checkbox" checked={constraint} disabled={busy || disabled} onChange={e => setConstraint(e.target.checked)} /><span>作为固定约束</span></label><p className="knowledge-muted">保存设置也会创建新版本，历史原文保留。</p><Button disabled={busy || disabled || !title.trim()} type="submit">{busy ? "保存中…" : "保存资料设置"}</Button></form></details>
    <details className="knowledge-edit-details"><summary>上传替换版本</summary><DocumentUpload libraryId={detail.library_id} existing={detail} disabled={busy || disabled} onError={onError} onUploaded={async () => { onChange(await getKnowledgeDocument(detail.document_id)); await onRefresh(); }} /></details>
  </div>;
}

export function KnowledgePanel({ libraryId, onSelectLibrary }: { libraryId?: string; onSelectLibrary: (id?: string) => void }) {
  const [libraries, setLibraries] = useState<KnowledgeLibrary[] | null>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[] | null>(null);
  const [detail, setDetail] = useState<KnowledgeDetail | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [category, setCategory] = useState("all");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<KnowledgeSearch | null>(null);
  const [searching, setSearching] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const library = libraries?.find(item => item.library_id === libraryId);
  const refresh = useCallback(async () => {
    const operations = [listKnowledgeLibraries(), libraryId ? listKnowledgeDocuments(libraryId) : Promise.resolve(null)];
    const [libs, docs] = await Promise.all(operations);
    setLibraries(libs as KnowledgeLibrary[]); setDocuments(docs as KnowledgeDocument[] | null);
  }, [libraryId]);
  useEffect(() => {
    let cancelled = false;
    Promise.all([listKnowledgeLibraries(), libraryId ? listKnowledgeDocuments(libraryId) : Promise.resolve(null)]).then(([libs, docs]) => { if (!cancelled) { setLibraries(libs); setDocuments(docs); } }).catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : "知识库加载失败"); });
    return () => { cancelled = true; };
  }, [libraryId]);
  async function action(work: () => Promise<void>) { setBusy(true); setError(""); setNotice(""); try { await work(); } catch (e) { setError(e instanceof Error ? e.message : "操作失败，请重试"); } finally { setBusy(false); } }
  async function openDocument(id: string) { await action(async () => { setDetail(await getKnowledgeDocument(id)); setUploadOpen(false); }); }
  async function remove() {
    if (!confirmDelete) return;
    await action(async () => {
      if (confirmDelete === "library" && libraryId) { await deleteKnowledgeLibrary(libraryId); onSelectLibrary(undefined); }
      else { await deleteKnowledgeDocument(confirmDelete); if (detail?.document_id === confirmDelete) setDetail(null); await refresh(); }
      setConfirmDelete(null); setNotice("已删除");
    });
  }
  async function retrieve(e: React.FormEvent) { e.preventDefault(); if (!libraryId || !query.trim()) return; setSearching(true); setError(""); setSearch(null); try { setSearch(await searchKnowledgeLibrary(libraryId, query.trim())); } catch (e) { setError(e instanceof Error ? e.message : "检索失败，请重试"); } finally { setSearching(false); } }
  const filteredDocuments = documents?.filter(document => category === "all" || document.category === category);
  return <section className="studio-section knowledge-page">
    <div className="studio-section-header"><div><p className="section-kicker">YOUR CREATIVE REFERENCE LIBRARY</p><h1>创作知识库</h1><p>故事短视频与漫剧共用资料，每个项目独立选择引用的知识库。</p></div><button className="cinema-primary" disabled={busy} onClick={() => setCreateOpen(!createOpen)}><FolderPlus size={14} />新建知识库</button></div>
    {error && <div className="knowledge-feedback error" role="alert">{error}<button className="text-link" onClick={() => void action(refresh)}>重新加载</button></div>}
    {notice && <p className="knowledge-feedback" role="status">{notice}</p>}
    {createOpen && <div className="studio-panel knowledge-create-panel"><div className="knowledge-inline-heading"><h2>建立新的创作资料空间</h2><button className="text-link" onClick={() => setCreateOpen(false)}>收起</button></div><LibraryForm disabled={busy} onSave={(name, description) => action(async () => { const created = await createKnowledgeLibrary(name, description); setCreateOpen(false); onSelectLibrary(created.library_id); })} /></div>}
    <div className="knowledge-layout">
      <aside className="studio-panel knowledge-library-list"><div className="knowledge-inline-heading"><h2><BookOpen size={15} />我的知识库</h2><span>{libraries?.length ?? "—"}</span></div>{libraries === null ? <p className="knowledge-muted">{error ? "知识库暂时无法加载。" : "正在加载…"}</p> : !libraries.length ? <div className="knowledge-empty"><BookOpen size={25} /><p>为你的第一个故事<br />整理一份创作资料。</p><button className="text-link" onClick={() => setCreateOpen(true)}>新建知识库 ↗</button></div> : libraries.map(item => <button key={item.library_id} className={"knowledge-library-row" + (item.library_id === libraryId ? " active" : "")} aria-pressed={item.library_id === libraryId} onClick={() => onSelectLibrary(item.library_id)}><strong>{item.name}</strong><span>{item.document_count} 份资料</span><small>{item.description || "创作参考资料"}</small></button>)}</aside>
      <div className="knowledge-content">
        {!libraryId ? <div className="studio-panel knowledge-welcome"><FileText size={30} /><p className="section-kicker">A PLACE FOR YOUR STORY WORLD</p><h2>让创作有自己的记忆。</h2><p>选择或创建知识库，导入角色设定、世界观和品牌规则。<br /></p><div><span>世界观</span><span>角色</span><span>剧情</span><span>风格</span><span>品牌</span></div></div> : <>
          <section className="studio-panel knowledge-library-heading"><div><p className="section-kicker">CREATIVE KNOWLEDGE</p><h2>{library?.name || "知识库"}</h2><p>{library?.description || "选择资料，查看原文和历史版本。"}</p></div><div className="knowledge-header-actions"><button className="text-link" disabled={busy} onClick={() => { setUploadOpen(!uploadOpen); setDetail(null); }}><Plus size={13} />新增资料</button><button className="knowledge-delete" disabled={busy || !library} onClick={() => setConfirmDelete("library")}>删除知识库</button></div>{library && <details className="knowledge-edit-details"><summary>编辑知识库信息</summary><LibraryForm key={`${library.library_id}:${library.updated_at}`} library={library} disabled={busy} onSave={(name, description) => action(async () => { await updateKnowledgeLibrary(libraryId, name, description); await refresh(); setNotice("知识库信息已保存。"); })} /></details>}</section>
          {confirmDelete && <div className="knowledge-feedback error"><span>{confirmDelete === "library" ? "删除这个知识库？" : "删除这份资料？"}</span><div><Button disabled={busy} onClick={() => void remove()}>确认删除</Button><button className="text-link" disabled={busy} onClick={() => setConfirmDelete(null)}>取消</button></div></div>}
          {uploadOpen && <section className="studio-panel"><h3 className="knowledge-section-title"><Upload size={15} />导入创作资料</h3><DocumentUpload libraryId={libraryId} disabled={busy} onError={setError} onUploaded={async document => { await refresh(); setNotice("资料已导入并建立索引，可在故事创作中选择此知识库。"); setDetail(await getKnowledgeDocument(document.document_id)); setUploadOpen(false); }} /></section>}
          <section className="studio-panel knowledge-documents"><div className="knowledge-inline-heading"><h3>资料目录 <span>{documents?.length ?? "—"}</span></h3><select aria-label="按资料分类筛选" value={category} onChange={e => setCategory(e.target.value)}><option value="all">全部分类</option>{Object.entries(KNOWLEDGE_CATEGORIES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div>{documents === null ? <p className="knowledge-muted">{error ? "资料无法加载，请重试。" : "正在加载资料…"}</p> : !filteredDocuments?.length ? <div className="knowledge-empty-inline"><FileText size={19} /><p>{documents.length ? "这个分类还没有资料。" : "这个知识库还没有资料，先上传一份设定文档。"}</p></div> : <div className="knowledge-document-list">{filteredDocuments.map(document => <article key={document.document_id} className={detail?.document_id === document.document_id ? "active" : ""}><button disabled={busy} className="knowledge-document-open" onClick={() => void openDocument(document.document_id)}><FileText size={17} /><span><strong>{document.title}</strong><small>{KNOWLEDGE_CATEGORIES[document.category]} · 版本 {document.version_number} · {document.chunk_count} 个片段</small></span>{document.is_constraint && <em className="knowledge-constraint">固定约束</em>}</button><button disabled={busy} className="knowledge-delete" aria-label={`删除资料${document.title}`} onClick={() => setConfirmDelete(document.document_id)}>删除</button></article>)}</div>}</section>
          {detail && <section className="studio-panel"><div className="knowledge-inline-heading"><h3>资料原文与版本</h3><button className="text-link" onClick={() => setDetail(null)}>关闭</button></div><DocumentDetail key={`${detail.document_id}:${detail.version_id}`} detail={detail} onChange={setDetail} onRefresh={refresh} disabled={busy} onError={setError} /></section>}
          <section className="studio-panel knowledge-search"><h3 className="knowledge-section-title"><Search size={15} />搜索资料</h3><form onSubmit={e => void retrieve(e)}><Input value={query} maxLength={2000} disabled={searching} onChange={e => setQuery(e.target.value)} placeholder="例如：这座城市的能源规则是什么？" aria-label="检索资料的问题" /><Button type="submit" disabled={searching || !query.trim()}>{searching ? "检索中…" : "检索资料"}</Button></form>{search && <KnowledgeSources context={search} title="搜索结果" />}</section>
        </>}
      </div>
    </div>
  </section>;
}
