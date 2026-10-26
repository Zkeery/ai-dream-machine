import { api } from "./client";

export const TRIAL_BLOCK_MESSAGE = "名额已满，可查看样片并填写问卷";

export type TrialEvent = "invite_open" | "consent_ok" | "create_start" | "prompt_submit" | "estimate_shown" | "gen_done" | "gen_fail" | "quota_block" | "paywall_click" | "survey_done";
export type TrialPlan = { id: string; label: string; price_cny: number | null; included_videos: number; benefits: string; confirmation_required: boolean };
export type TrialStatus = {
  mode: string;
  consented: boolean;
  quota_remaining: number;
  generation_enabled: boolean;
  max_estimate_cny?: number;
  blocked_message?: string;
  plans?: TrialPlan[];
  estimated_charge_cny?: number;
  estimates?: { comic?: number; three_shot?: number };
  estimate_kind?: string;
  actual_charge_cny?: number;
};

export type SurveyFields = {
  participant_code: string;
  intent_raw: string;
  independent_completion: "yes" | "no";
  blocker_raw: string;
  need_fit: number;
  character_consistency: number;
  visual_quality: number;
  camera_motion: number;
  audio_subtitles: number | "n/a";
  satisfaction: number;
  recommendation: number;
  paywall_clicked: boolean;
  selected_plan: string;
  willing_monthly_raw: string;
  no_pay_reason_raw: string;
  other_raw: string;
};

export const getTrialStatus = () => api<TrialStatus>("/api/trial/status");
export const acceptTrialConsent = () => api<{ ok: boolean }>("/api/trial/consent", { method: "POST", body: JSON.stringify({ accepted: true }) });
export const recordTrialEvent = (event: TrialEvent, payload: Record<string, unknown> = {}) => api<{ ok: boolean }>("/api/trial/events", { method: "POST", body: JSON.stringify({ event, payload }) });
export const submitTrialSurvey = (fields: SurveyFields) => api<{ ok: boolean }>("/api/trial/survey", { method: "POST", body: JSON.stringify({ fields }) });
export const requestTrialDeletion = () => api<{ ok: boolean; request_id?: string }>("/api/trial/deletion-request", { method: "POST", body: JSON.stringify({}) });
