// 按路径部署时，API 与静态媒体一起留在本项目的同源路径下。
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
export const API_BASE = BASE_PATH || (process.env.NEXT_PUBLIC_API_URL ?? "");

export function publicPath(path: string): string {
  return `${BASE_PATH}${path}`;
}
