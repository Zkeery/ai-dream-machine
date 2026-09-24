import { api } from "./client";

export type Settings = {
  gateway: string;
  models: {
    llm: string;
    vlm: string;
    image_t2i: string;
    video_first_frame: string;
    video_reference: string;
  };
  content_review_enabled: boolean;
  token_ttl_days: number;
};

export function getSettings(): Promise<Settings> {
  return api<Settings>("/api/settings");
}
