import { api } from "./client";

export type AccountUsage = {
  scope: "account";
  month: string;
  currency: string;
  limit_cny: number;
  reserved_cny: number;
  calculated_cny: number;
  provider_reported_cny: number | null;
  remaining_cny: number;
  uncertain_calls: number;
  estimated_completed_calls: number;
  concurrency: { active: number; account_limit: number; global_limit: number };
  missing_price_models: string[];
  notice: string;
};

export function getAccountUsage(): Promise<AccountUsage> {
  return api("/api/usage");
}

export function cnyLabel(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "待核实";
}
