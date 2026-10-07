import type { KnowledgeLibrary } from "./api/knowledge";

export function toggleKnowledgeLibrary(current: string[], id: string, checked: boolean): string[] {
  const next = checked ? [...new Set([...current, id])] : current.filter(value => value !== id);
  if (next.length > 3) throw new Error("每个故事最多选择 3 个知识库");
  return next;
}
export function unavailableKnowledgeIds(ids: string[], libraries: KnowledgeLibrary[]): string[] {
  const known = new Set(libraries.map(library => library.library_id));
  return ids.filter(id => !known.has(id));
}
export function validateKnowledgeFile(file: { name: string; size: number }): string {
  if (!/\.(md|txt|pdf)$/i.test(file.name)) return "支持 Markdown、TXT 或可提取文字的 PDF 文件";
  if (!file.size) return "文件为空，请选择包含文字的资料";
  if (file.size > 5 * 1024 * 1024) return "每份资料不能超过 5 MB";
  return "";
}

export function knowledgeUploadForm(input: { file: File; title: string; category: string; is_constraint: boolean }): FormData {
  const form = new FormData();
  form.append("file", input.file);
  // An omitted optional title derives from filename. Empty strings are invalid server titles.
  if (input.title.trim()) form.append("title", input.title.trim());
  form.append("category", input.category);
  form.append("is_constraint", String(input.is_constraint));
  return form;
}
