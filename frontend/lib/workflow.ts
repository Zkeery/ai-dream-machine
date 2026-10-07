import type { SessionMeta } from "./api/sessions";
import type { TaskMeta } from "./api/tasks";
import type { ModelCatalog, ModelKey, ModelSelection } from "./api/models";

export type PipelineTab = "literary" | "motion" | "talking";
export type TalkingMode = "lip_sync" | "static";
export type ProjectType = "story" | "comic";
export type WorkspaceView = "create" | "projects" | "pipelines" | "knowledge" | "settings" | "sandbox";
export type WorkspaceRoute = { view: WorkspaceView; sessionId?: string; taskId?: string; tool?: PipelineTab; libraryId?: string; projectType?: ProjectType };
export type StoryDraft = { idea: string; style: string; episodes: number; episodeDefaultVersion?: 1; ratio: string; knowledgeLibraryIds: string[]; modelSelection?: ModelSelection; orchestrationMode?: "workflow" | "multi_agent" };
export type ToolDraft = { text: string; style: string; image: string; imageName: string; motion: string; motionName: string; assetSource: string; talkingMode?: TalkingMode; modelSelection?: ModelSelection };
export type WorkspaceDraft = { version: 1; tool: PipelineTab; story: StoryDraft; comic: StoryDraft; tools: Record<PipelineTab, ToolDraft> };

export const PROJECT_LABELS: Record<ProjectType, string> = { story: "故事短视频", comic: "漫剧创作" };
export const PROJECT_STAGES: Record<ProjectType, string[]> = {
  story: ["script_generation", "character_design", "storyboard", "reference_generation", "video_generation", "post_production"],
  comic: ["script_generation", "character_design", "comic_storyboard", "comic_panels", "comic_audio", "comic_composition"],
};
export function projectTypeOf(session: Pick<SessionMeta, "project_type">): ProjectType { return session.project_type === "comic" ? "comic" : "story"; }
export function finalStageOf(session: Pick<SessionMeta, "project_type">): string { return projectTypeOf(session) === "comic" ? "comic_composition" : "post_production"; }
export function finalVideoOf(session: Pick<SessionMeta, "project_type" | "artifacts">): string {
  const artifact = session.artifacts?.[finalStageOf(session)];
  return artifact && typeof artifact === "object" && "final_video" in artifact && typeof artifact.final_video === "string" ? artifact.final_video : "";
}
export function libraryMatchesFilter(item: LibraryItem, filter: string): boolean { return filter === "all" || (filter === "task" ? item.kind === "task" : item.kind === "session" && item.projectType === filter); }
export function creationInput(draft: StoryDraft, projectType: ProjectType) {
  const selection = applicableModelSelection(draft.modelSelection, creationModelKeys(projectType));
  return { idea: draft.idea.trim(), style: draft.style.trim(), episodes: projectType === "comic" ? 1 : draft.episodes, video_ratio: draft.ratio, knowledge_library_ids: draft.knowledgeLibraryIds, project_type: projectType, ...(draft.orchestrationMode ? { orchestration_mode: draft.orchestrationMode } : {}), ...(Object.keys(selection).length ? { model_selection: selection } : {}) };
}

export const MODEL_KEYS: ModelKey[] = ["text", "image", "video_first_frame", "video_start_end", "video_reference", "video_speech"];
export function normalizeModelSelection(value: unknown): ModelSelection {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const record = value as Record<string, unknown>, selection: ModelSelection = {};
  for (const key of MODEL_KEYS) if (typeof record[key] === "string" && record[key].trim()) selection[key] = record[key].trim();
  return selection;
}
export function creationModelKeys(type: ProjectType): ModelKey[] { return type === "comic" ? ["text", "image"] : ["text", "image", "video_first_frame"]; }
export function toolModelKeys(tool: PipelineTab, talkingMode?: unknown): ModelKey[] { return tool === "literary" ? ["text", "image"] : tool === "motion" ? ["video_reference"] : tool === "talking" && talkingMode === "lip_sync" ? ["video_speech"] : []; }
export function stageModelKeys(stage: string, videoMode: string): ModelKey[] {
  if (["script_generation", "storyboard", "comic_storyboard"].includes(stage)) return ["text"];
  if (["character_design", "reference_generation", "comic_panels"].includes(stage)) return ["image"];
  if (stage === "video_generation") return videoMode === "reference" ? ["video_reference"] : videoMode === "start_end" ? ["video_start_end"] : ["video_first_frame"];
  return [];
}
export function applicableModelSelection(selection: unknown, keys: ModelKey[]): ModelSelection {
  const normalized = normalizeModelSelection(selection), result: ModelSelection = {};
  for (const key of keys) if (normalized[key]) result[key] = normalized[key];
  return result;
}
export function resolvedModelSelection(selection: unknown, catalog: ModelCatalog | null, keys: ModelKey[]): ModelSelection {
  const result = applicableModelSelection(selection, keys);
  for (const key of keys) if (!result[key] && catalog?.groups[key]?.default) result[key] = catalog.groups[key].default!;
  return result;
}
export function modelSelectionError(selection: ModelSelection, catalog: ModelCatalog | null, keys: ModelKey[]): string {
  if (!keys.length) return "";
  if (!catalog) return "请先加载可用模型，再提交生成。";
  for (const key of keys) {
    const group = catalog.groups[key];
    if (!group?.options.length) return `${group?.label ?? (key === "video_speech" ? "嘴型同步视频" : key)}模型暂未开放，当前无法生成。`;
    if (!selection[key]) return `请选择${group.label}模型，再提交生成。`;
    if (!group.options.some(option => option.id === selection[key])) return `已保存的${group.label}模型 ${selection[key]} 当前不可用，请明确选择其他模型。`;
  }
  return "";
}
export function projectStageState(session: SessionMeta | null, viewStage?: string) {
  const projectType = session ? projectTypeOf(session) : "story", stageOrder = PROJECT_STAGES[projectType];
  const finalStage = stageOrder[stageOrder.length - 1];
  const stage = viewStage && stageOrder.includes(viewStage) ? viewStage : session?.current_stage ?? finalStage;
  const stale = session?.stale_stages?.includes(stage) ?? false;
  const blocked = Boolean(session && stageOrder.slice(0, stageOrder.indexOf(stage)).some(previous => !session.stages_completed.includes(previous) || session.stale_stages?.includes(previous)));
  const canConfirm = Boolean(session && stage === session.current_stage && session.status === "stage_completed" && !stale && stage !== finalStage);
  return { projectType, stageOrder, finalStage, stage, stale, blocked, canConfirm };
}

export function videoFormatError(selection: ModelSelection, catalog: ModelCatalog | null, keys: ModelKey[], ratio: string, resolution: string): string {
  for (const key of keys) {
    const option = catalog?.groups[key]?.options.find(item => item.id === selection[key]);
    if (option?.video && (!option.video.ratios.includes(ratio) || !option.video.resolutions.includes(resolution.toUpperCase())))
      return `${option.label} 支持 ${option.video.ratios.join("、")} 与 ${option.video.resolutions.join("/")}，当前作品为 ${ratio} ${resolution}，请选择兼容模型。`;
  }
  return "";
}

export const TOOL_LABELS: Record<PipelineTab, string> = { literary: "文艺短视频", motion: "角色动作短片", talking: "图片配音口播" };
export const TALKING_MODE_LABELS: Record<TalkingMode, string> = { lip_sync: "人物嘴型同步", static: "静态图片配音" };
/** Missing modes belong to historical static tasks and saved drafts. */
export function talkingModeOf(value: unknown): TalkingMode { return value === "lip_sync" ? "lip_sync" : "static"; }
export function talkingScriptLength(text: string): number { return Array.from(text.replace(/\s/gu, "")).length; }
export const TASK_TO_TOOL: Record<string, PipelineTab> = { literary_video: "literary", motion_transfer: "motion", talking_head: "talking" };
export const STATUS_LABELS: Record<string, string> = { idle: "待生成", pending: "排队中", running: "生成中", stage_completed: "等待确认", session_completed: "已完成", completed: "已完成", failed: "生成失败", interrupted: "已中断" };

export function emptyDraft(): WorkspaceDraft {
  const tool = (): ToolDraft => ({ text: "", style: "realistic", image: "", imageName: "", motion: "", motionName: "", assetSource: "" });
  return { version: 1, tool: "literary", story: { idea: "", style: "realistic", episodes: 1, episodeDefaultVersion: 1, orchestrationMode: "multi_agent", ratio: "16:9", knowledgeLibraryIds: [] }, comic: { idea: "", style: "anime", episodes: 1, orchestrationMode: "multi_agent", ratio: "9:16", knowledgeLibraryIds: [] }, tools: { literary: tool(), motion: tool(), talking: { ...tool(), talkingMode: "lip_sync" } } };
}

export function normalizeKnowledgeIds(value: unknown): string[] {
  return Array.isArray(value) ? [...new Set(value.filter((id): id is string => typeof id === "string" && Boolean(id.trim())).map(id => id.trim()))].slice(0, 3) : [];
}

export function draftKey(userId: string): string { return `dream-machine:draft:v1:${encodeURIComponent(userId)}`; }

/** Only this account's text and safe server upload references are stored. Never tokens or local File blobs. */
export function readDraft(storage: Pick<Storage, "getItem">, userId: string): WorkspaceDraft {
  const fallback = emptyDraft();
  try {
    const raw: unknown = JSON.parse(storage.getItem(draftKey(userId)) ?? "null");
    if (!raw || typeof raw !== "object" || !("version" in raw) || raw.version !== 1) return fallback;
    const value = raw as Partial<WorkspaceDraft>;
    if (value.tool && Object.hasOwn(TOOL_LABELS, value.tool)) fallback.tool = value.tool;
    for (const kind of ["story", "comic"] as const) {
      const saved = value[kind];
      if (!saved || typeof saved !== "object") continue;
      fallback[kind].orchestrationMode = saved.orchestrationMode === "multi_agent" ? "multi_agent" : "workflow";
      for (const field of ["idea", "style", "ratio"] as const) if (typeof saved[field] === "string") fallback[kind][field] = saved[field];
      if (kind === "story" && typeof saved.episodes === "number" && Number.isFinite(saved.episodes)) {
        // Old drafts cannot distinguish the former four-episode default from a choice.
        // Migrate that unversioned value once; subsequent choices (including 4) persist.
        fallback.story.episodes = saved.episodes === 4 && saved.episodeDefaultVersion !== 1 ? 1 : saved.episodes;
      }
      fallback[kind].knowledgeLibraryIds = normalizeKnowledgeIds(saved.knowledgeLibraryIds);
      if (saved.modelSelection !== undefined) fallback[kind].modelSelection = applicableModelSelection(saved.modelSelection, kind === "comic" ? ["text", "image"] : MODEL_KEYS.filter(key => key !== "video_speech"));
    }
    for (const tool of Object.keys(TOOL_LABELS) as PipelineTab[]) {
      const saved = value.tools?.[tool];
      if (!saved || typeof saved !== "object") continue;
      for (const field of ["text", "style", "image", "imageName", "motion", "motionName", "assetSource"] as const) if (typeof saved[field] === "string") fallback.tools[tool][field] = saved[field];
      if (tool === "talking") fallback.tools[tool].talkingMode = talkingModeOf(saved.talkingMode);
      if (saved.modelSelection !== undefined) fallback.tools[tool].modelSelection = applicableModelSelection(saved.modelSelection, toolModelKeys(tool, fallback.tools[tool].talkingMode));
    }
  } catch { /* Invalid/old storage is a new draft, not a broken page. */ }
  return fallback;
}

export function carryStoryToTool(draft: WorkspaceDraft, tool: PipelineTab, projectType: ProjectType = "story"): WorkspaceDraft {
  // Episode count and ratio belong to stories. Current shortcuts cannot honor them.
  const source = draft[projectType];
  const text = source.idea.trim() ? source.idea : draft.tools[tool].text;
  return { ...draft, tool, tools: { ...draft.tools, [tool]: { ...draft.tools[tool], text, ...(tool === "literary" ? { style: source.style } : {}) } } };
}

export function parseRoute(params: Pick<URLSearchParams, "get">, fallbackTool: PipelineTab = "literary"): WorkspaceRoute {
  const requested = params.get("view");
  const view: WorkspaceView = requested && ["create", "projects", "pipelines", "knowledge", "settings", "sandbox"].includes(requested) ? requested as WorkspaceView : "create";
  if (view === "create") return { view, sessionId: params.get("session") || undefined, ...(params.get("project") === "comic" ? { projectType: "comic" as const } : {}) };
  if (view === "pipelines") {
    const tool = params.get("tool");
    return { view, taskId: params.get("task") || undefined, tool: tool && Object.hasOwn(TOOL_LABELS, tool) ? tool as PipelineTab : fallbackTool };
  }
  if (view === "knowledge") return { view, libraryId: params.get("library") || undefined };
  return { view };
}

export function routeUrl(route: WorkspaceRoute): string {
  const params = new URLSearchParams();
  if (route.view !== "create") params.set("view", route.view);
  if (route.view === "create" && route.sessionId) params.set("session", route.sessionId);
  if (route.view === "create" && route.projectType === "comic") params.set("project", "comic");
  if (route.view === "pipelines") {
    if (route.tool) params.set("tool", route.tool);
    if (route.taskId) params.set("task", route.taskId);
  }
  if (route.view === "knowledge" && route.libraryId) params.set("library", route.libraryId);
  return params.size ? `/?${params}` : "/";
}

export type LibraryItem = { key: string; id: string; kind: "session" | "task"; projectType?: ProjectType; title: string; description: string; source: string; status: string; updatedAt: number; session?: SessionMeta; task?: TaskMeta };
export function titleOfSession(s: SessionMeta): string {
  const script = s.artifacts?.script_generation;
  return script && typeof script === "object" && "title" in script && typeof script.title === "string" && script.title ? script.title : s.idea.slice(0, 24) || "未命名故事";
}
export function libraryItems(sessions: SessionMeta[], tasks: TaskMeta[]): LibraryItem[] {
  return [...sessions.map(s => ({ key: `session:${s.session_id}`, id: s.session_id, kind: "session" as const, projectType: projectTypeOf(s), title: titleOfSession(s), description: s.idea, source: PROJECT_LABELS[projectTypeOf(s)], status: s.status, updatedAt: s.updated_at, session: s })), ...tasks.map(t => {
    const text = String(t.input?.text ?? t.input?.script ?? t.input?.prompt ?? "");
    const source = taskLabel(t);
    return { key: `task:${t.task_id}`, id: t.task_id, kind: "task" as const, title: text.slice(0, 24) || source, description: text, source, status: taskResultError(t) ? "failed" : t.status, updatedAt: t.updated_at, task: t };
  })].sort((a, b) => b.updatedAt - a.updatedAt || a.key.localeCompare(b.key));
}

export function taskIsActive(status: string): boolean { return status === "pending" || status === "running"; }

export function taskLabel(task: TaskMeta): string {
  const label = TOOL_LABELS[TASK_TO_TOOL[task.type]] ?? "快捷短片";
  return task.type === "talking_head" ? `${label} · ${TALKING_MODE_LABELS[talkingModeOf(task.input.talking_mode)]}` : label;
}

/** A completed static result must never stand in for a requested speaking portrait. */
export function taskResultError(task: TaskMeta): string {
  return task.type === "talking_head" && task.status === "completed" && talkingModeOf(task.input.talking_mode) === "lip_sync" && task.result?.talking_mode !== "lip_sync"
    ? "未取得人物嘴型同步成片，当前结果无法预览或下载。输入已保留，请重新生成；如需静态视频，请主动选择静态图片配音。" : "";
}
export function taskCanPreview(task: TaskMeta): boolean { return task.status === "completed" && !taskResultError(task); }

/** Task records contain resolved disk paths; upload inputs must use the registered basename. */
export function uploadFilename(value: unknown): string {
  if (typeof value !== "string") return "";
  const filename = value.split(/[\\/]/).pop() || "";
  return /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(filename) ? filename : "";
}

export function toolDraftFromTask(task: TaskMeta): ToolDraft {
  const input = task.input;
  const image = uploadFilename(input.character_image ?? input.person_image);
  const motion = uploadFilename(input.motion_video);
  const talkingMode = talkingModeOf(input.talking_mode);
  const selection = applicableModelSelection(input.model_selection, toolModelKeys(TASK_TO_TOOL[task.type], talkingMode));
  return { text: String(input.text ?? input.prompt ?? input.script ?? ""), style: String(input.style ?? "realistic"), image, imageName: image, motion, motionName: motion, assetSource: typeof input.asset_source === "object" && input.asset_source && "asset_id" in input.asset_source ? String(input.asset_source.asset_id) : "", ...(task.type === "talking_head" ? { talkingMode } : {}), ...(Object.keys(selection).length ? { modelSelection: selection } : {}) };
}

export function toolTaskInput(tab: PipelineTab, draft: ToolDraft): Record<string, unknown> {
  const source = draft.assetSource ? { asset_source: { asset_id: draft.assetSource } } : {};
  const talkingMode = talkingModeOf(draft.talkingMode);
  const selection = applicableModelSelection(draft.modelSelection, toolModelKeys(tab, talkingMode));
  const models = Object.keys(selection).length ? { model_selection: selection } : {};
  // Historical motion references remain in drafts/tasks, but this workflow uses only image + prompt.
  return tab === "literary" ? { text: draft.text.trim(), style: draft.style.trim(), ...models } : tab === "motion" ? { character_image: uploadFilename(draft.image), prompt: draft.text.trim(), ...source, ...models } : { person_image: uploadFilename(draft.image), script: draft.text.trim(), talking_mode: talkingMode, ...source, ...models };
}

export function stageGenerationRequest(stage: string, artifact: unknown, currentMode: string, chosenMode: string): { kind: "execute" } | { kind: "intervene"; modifications: Record<string, unknown> } {
  // Existing results are versioned interventions, including a finished film.
  if (artifact || (stage === "video_generation" && chosenMode !== currentMode)) return { kind: "intervene", modifications: { operation: "regenerate", ...(stage === "video_generation" ? { video_generation_mode: chosenMode } : {}) } };
  return { kind: "execute" };
}

export function targetedGenerationRequest(stage: string, targetIds: string[], knownIds: string[], description?: string): Record<string, unknown> {
  if (!["character_design", "reference_generation", "video_generation", "comic_panels", "comic_audio"].includes(stage)) throw new Error("此阶段不支持按素材生成");
  const targets = [...new Set(targetIds)];
  if (!targets.length || targets.some(id => !knownIds.includes(id))) throw new Error("请选择当前阶段有效的素材或镜头");
  const result: Record<string, unknown> = { operation: "regenerate", target_ids: targets };
  if (description !== undefined) {
    if (stage !== "character_design" || targets.length !== 1 || !description.trim() || description.trim().length > 4000) throw new Error("请为单个角色或场景填写 1–4000 字的提示词");
    result.prompts = { [targets[0]]: description.trim() };
    result.descriptions = { [targets[0]]: description.trim() };
  }
  return result;
}

export function pendingGenerationTargets(artifact: unknown, knownIds: string[]): string[] {
  if (!artifact || typeof artifact !== "object" || !("stale_items" in artifact) || !Array.isArray(artifact.stale_items)) return [];
  return [...new Set(artifact.stale_items.filter((id): id is string => typeof id === "string" && knownIds.includes(id)))];
}
