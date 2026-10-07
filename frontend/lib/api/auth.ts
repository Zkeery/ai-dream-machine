import { api } from "./client";

export type LoginResult = {
  token: string;
  user_id: string;
  expires_at: number;
  is_admin: boolean;
};

export type Me = {
  user_id: string;
  invite_code: string;
  created_at: number;
  is_admin: boolean;
};

export function login(invite_code: string): Promise<LoginResult> {
  return api<LoginResult>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ invite_code }),
  });
}

export function me(): Promise<Me> {
  return api<Me>("/api/auth/me");
}

export function logout(): Promise<{ ok: boolean }> {
  return api<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
}
