import { api } from "./client";

export type SandboxStatus = {
  status: string;
  session_count: number;
  task_count: number;
};

export function getSandboxStatus(): Promise<SandboxStatus> {
  return api<SandboxStatus>("/api/admin/sandbox");
}
