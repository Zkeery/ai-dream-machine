// 默认通过同源 /api 代理访问后端，公网访客不会请求自己的 localhost。
export const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";
