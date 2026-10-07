import { api, ApiError, authHeaders } from "./client";
import { API_BASE } from "../config";
import { streamSSE, type SSEEvent } from "../stream";
import type { KnowledgeContext } from "./knowledge";
import type { ProjectType } from "../workflow";
import type { ModelSelection, ModelUsage } from "./models";

export type SessionMeta = {
  session_id: string;
  orchestration_mode?: "workflow" | "multi_agent";
  agent_runs?: Record<string, AgentRun[]>;
  project_type?: ProjectType;
  model_selection?: ModelSelection;
  owner_id: string | null;
  idea: string;
  style: string;
  episodes: number;
  video_ratio: string;
  resolution: string;
  expand_idea: boolean;
  video_generation_mode: string;
  status: string;
  current_stage: string | null;
  stages_completed: string[];
  artifacts: Record<string, unknown>;
  error: string | null;
  created_at: number;
  updated_at: number;
  execution?: { execution_id?: string; status: string; stage?: string; started_at?: number; updated_at?: number; last_event?: { message?: string; percent?: number } | null; error?: string | null; model_usage?: ModelUsage };
  execution_inputs?: { model_usage?: ModelUsage; [key: string]: unknown }[];
  stale_stages?: string[];
  selected_versions?: Record<string, string>;
  artifact_versions?: Record<string, { version_id: string; artifact: unknown; input_versions: Record<string, string>; created_at: number; reason: string; model_usage?: ModelUsage }[]>;
  knowledge_library_ids?: string[];
  knowledge_status?: { changed: boolean; warnings: string[] };
};

export type ScriptCharacter = { name: string; character_id: string; description: string; role: string };
export type ScriptSetting = { name: string; setting_id: string; description: string };
export type ScriptEpisode = { episode_number: number; act_title: string; content: string };
export type ScriptArtifact = {
  knowledge_context?: KnowledgeContext;
  title: string;
  logline: string;
  genre: string[];
  mood: string;
  characters: ScriptCharacter[];
  settings: ScriptSetting[];
  episodes: ScriptEpisode[];
};

export type CreateSessionInput = {
  orchestration_mode?: "workflow" | "multi_agent";
  project_type?: ProjectType;
  model_selection?: ModelSelection;
  knowledge_library_ids?: string[];
  idea: string;
  style?: string;
  episodes?: number;
  video_ratio?: string;
};

export type AgentRun = {
  execution_id: string; status: string; started_at: number; decisions: number;
  plan?: { summary: string; tasks: { id: string; role: string; objective: string; depends_on: string[] }[] };
  tasks: Record<string, { status: string; handoff?: string }>;
  events: { role: string; action: string; summary: string; at: number }[];
  reviews: { verdict: string; summary: string }[];
};

export function listSessions(): Promise<SessionMeta[]> {
  return api<SessionMeta[]>("/api/sessions");
}

export function createSession(input: CreateSessionInput): Promise<SessionMeta> {
  return api<SessionMeta>("/api/sessions", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getSession(sessionId: string): Promise<SessionMeta> {
  return api<SessionMeta>(`/api/sessions/${sessionId}`);
}

/** DELETE returns no body on 204; keep the shared JSON client unchanged. */
export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE", headers: authHeaders() });
  if (response.ok) return;
  let code = "DELETE_FAILED", message = "作品删除失败，请重试";
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "error" in body && body.error && typeof body.error === "object") {
      if ("code" in body.error && typeof body.error.code === "string") code = body.error.code;
      if ("message" in body.error && typeof body.error.message === "string") message = body.error.message;
    }
  } catch { /* A non-JSON error still has a clear retry message. */ }
  throw new ApiError(code, message, response.status);
}

export function executeStage(
  sessionId: string,
  stage: string,
  onEvent: (ev: SSEEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamSSE(`/api/sessions/${sessionId}/execute/${stage}`, { method: "POST", signal }, onEvent);
}

export function getArtifact<T>(sessionId: string, stage: string): Promise<T> {
  return api<T>(`/api/sessions/${sessionId}/artifact/${stage}`);
}

export function continueSession(sessionId: string): Promise<SessionMeta> {
  return api<SessionMeta>(`/api/sessions/${sessionId}/continue`, { method: "POST" });
}

export function interveneSession(sessionId: string, stage: string, modifications: Record<string, unknown>, onEvent: (ev: SSEEvent) => void): Promise<void> {
  return streamSSE(`/api/sessions/${sessionId}/intervene`, { method: "POST", body: JSON.stringify({ stage, modifications }) }, onEvent);
}

export function updateSessionKnowledge(sessionId: string, libraryIds: string[]): Promise<SessionMeta> {
  return api(`/api/sessions/${encodeURIComponent(sessionId)}/knowledge`, { method: "PATCH", body: JSON.stringify({ knowledge_library_ids: libraryIds }) });
}

export function updateSessionModels(sessionId: string, selection: ModelSelection): Promise<SessionMeta> {
  return api(`/api/sessions/${encodeURIComponent(sessionId)}/models`, { method: "PATCH", body: JSON.stringify({ model_selection: selection }) });
}

// ---- 各阶段产物类型 ----

export type DesignItem = { id: string; name: string; description: string; selected: string; versions: string[]; model_usage?: ModelUsage };
export type CharacterDesignArtifact = { characters: DesignItem[]; settings: DesignItem[]; stale_items?: string[] };
export type StoryboardShot = { shot_id: string; episode_number: number; description: string; prompt: string; character_ids?: string[]; setting_ids?: string[] };
export type StoryboardArtifact = { shots: StoryboardShot[]; knowledge_context?: KnowledgeContext };
export type ReferenceShot = { shot_id: string; path: string; selected?: string; versions?: string[]; input_paths?: string[]; model_usage?: ModelUsage };
export type ReferenceArtifact = { shots: ReferenceShot[]; stale_items?: string[] };
export type VideoSegment = { segment_id: string; shot_id: string; path: string; prompt: string; selected?: string; versions?: string[]; input_paths?: string[]; model_usage?: ModelUsage };
export type VideoArtifact = { segments: VideoSegment[]; mode: string; tail_frames?: Record<string, string>; stale_items?: string[] };
export type PostProductionArtifact = { final_video: string; parts: string[] };
