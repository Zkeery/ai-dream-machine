import { api } from "./client";

export type ModelKey = "text" | "image" | "video_first_frame" | "video_start_end" | "video_reference" | "video_speech";
export type ModelSelection = Partial<Record<ModelKey, string>>;
export type ModelOption = { id: string; label: string; description: string; video?: { duration_seconds: number; ratios: string[]; resolutions: string[]; max_reference_images: number | null; estimated_clip_cny: Record<string, number> } };
export type ModelGroup = { label: string; default: string | null; options: ModelOption[] };
export type ModelCatalog = { provider: string; groups: Record<ModelKey, ModelGroup> };
export type ModelUsage = { provider: string; models: ModelSelection };

/** The authenticated catalog contains public capabilities, never provider credentials. */
export async function getModelCatalog(): Promise<ModelCatalog> {
  const catalog = await api<ModelCatalog>("/api/models");
  // Older catalogs keep their existing capabilities; speech generation stays unavailable.
  if (catalog?.groups && !catalog.groups.video_speech) catalog.groups.video_speech = { label: "嘴型同步视频", default: null, options: [] };
  const keys: ModelKey[] = ["text", "image", "video_first_frame", "video_start_end", "video_reference", "video_speech"];
  if (!catalog || typeof catalog.provider !== "string" || !catalog.groups || keys.some(key => {
    const group = catalog.groups[key];
    return !group || typeof group.label !== "string" || (group.default !== null && typeof group.default !== "string") || !Array.isArray(group.options)
      || group.options.some(option => !option || typeof option.id !== "string" || !option.id || typeof option.label !== "string" || typeof option.description !== "string")
      || (group.default !== null && !group.options.some(option => option.id === group.default));
  })) throw new Error("模型目录不完整，请重新加载。尚未提交生成请求。");
  return catalog;
}
