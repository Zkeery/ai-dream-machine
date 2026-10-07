import { api } from "./client";
import type { KnowledgeContext } from "./knowledge";
import type { ModelUsage } from "./models";

export type ComicMotion = "static" | "push_in" | "pan_left" | "pan_right";
export type ComicDialogue = { line_id: string; speaker_id: string; text: string; emotion: string };
export type ComicShot = { shot_id: string; episode_number: number; description: string; prompt: string; character_ids: string[]; setting_ids: string[]; motion: ComicMotion; silent_duration: number; dialogues: ComicDialogue[] };
export type ComicStoryboardArtifact = { shots: ComicShot[]; voice_map: Record<string, string>; knowledge_context?: KnowledgeContext };
export type ComicPanelsArtifact = { shots: { shot_id: string; path: string; selected: string; versions: string[]; model_usage?: ModelUsage }[]; stale_items?: string[] };
export type ComicAudioArtifact = { shots: { shot_id: string; lines: { line_id: string; speaker_id: string; text: string; voice: string; path: string }[] }[]; stale_items?: string[] };
export type ComicCompositionArtifact = { final_video: string; subtitle_path?: string; duration: number; width: number; height: number; fps: number; segments: { shot_id: string; path: string; srt_path?: string; duration: number }[]; parts: string[]; stale_items?: string[] };
export type ComicVoice = { voice: string; name: string; gender: string };

export function getComicVoices(): Promise<{ voices: ComicVoice[]; emotion_supported: boolean }> { return api("/api/comic/voices"); }
