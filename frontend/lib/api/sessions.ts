import { api } from "./client";
import { streamSSE, type SSEEvent } from "../stream";

export type SessionMeta = {
  session_id: string;
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
};

export type ScriptCharacter = { name: string; character_id: string; description: string; role: string };
export type ScriptSetting = { name: string; setting_id: string; description: string };
export type ScriptEpisode = { episode_number: number; act_title: string; content: string };
export type ScriptArtifact = {
  title: string;
  logline: string;
  genre: string[];
  mood: string;
  characters: ScriptCharacter[];
  settings: ScriptSetting[];
  episodes: ScriptEpisode[];
};

export type CreateSessionInput = {
  idea: string;
  style?: string;
  episodes?: number;
  video_ratio?: string;
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

export function executeStage(
  sessionId: string,
  stage: string,
  onEvent: (ev: SSEEvent) => void,
): Promise<void> {
  return streamSSE(`/api/sessions/${sessionId}/execute/${stage}`, { method: "POST" }, onEvent);
}

export function getArtifact<T>(sessionId: string, stage: string): Promise<T> {
  return api<T>(`/api/sessions/${sessionId}/artifact/${stage}`);
}

export function continueSession(sessionId: string): Promise<SessionMeta> {
  return api<SessionMeta>(`/api/sessions/${sessionId}/continue`, { method: "POST" });
}

// ---- 各阶段产物类型 ----

export type DesignItem = { id: string; name: string; description: string; selected: string; versions: string[] };
export type CharacterDesignArtifact = { characters: DesignItem[]; settings: DesignItem[] };
export type StoryboardShot = { shot_id: string; episode_number: number; description: string; prompt: string };
export type StoryboardArtifact = { shots: StoryboardShot[] };
export type ReferenceShot = { shot_id: string; path: string };
export type ReferenceArtifact = { shots: ReferenceShot[] };
export type VideoSegment = { segment_id: string; shot_id: string; path: string; prompt: string };
export type VideoArtifact = { segments: VideoSegment[]; mode: string };
export type PostProductionArtifact = { final_video: string; parts: string[] };
