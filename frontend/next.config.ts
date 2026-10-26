import type { NextConfig } from "next";

const BACKEND_URL = process.env.BACKEND_URL || "http://127.0.0.1:8030";
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH || "";
if (BASE_PATH && !/^\/[a-z0-9-]+$/.test(BASE_PATH)) {
  throw new Error("NEXT_PUBLIC_BASE_PATH must be a single safe path segment");
}

const nextConfig: NextConfig = {
  turbopack: { root: process.cwd() },
  output: "standalone",
  basePath: BASE_PATH,
  trailingSlash: Boolean(BASE_PATH),
  agentRules: false,
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
