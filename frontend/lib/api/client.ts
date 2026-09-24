import { API_BASE } from "../config";

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

const TOKEN_KEY = "zmj_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function toApiError(res: Response): Promise<ApiError> {
  let code = "UNKNOWN_ERROR";
  let message = "请求失败，请重试";
  try {
    const body = await res.json();
    if (body?.error) {
      code = body.error.code || code;
      message = body.error.message || message;
    }
  } catch {
    // 非 JSON 响应，保留默认文案
  }
  return new ApiError(code, message, res.status);
}

/** 统一 JSON 请求：自动带鉴权头、错误归一化；401 抛 ApiError(status=401)。 */
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  const auth = authHeaders();
  for (const [k, v] of Object.entries(auth)) headers.set(k, v);
  if (options.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!res.ok) {
    throw await toApiError(res);
  }
  return (await res.json()) as T;
}
