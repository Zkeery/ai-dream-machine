import { cpSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const server = path.join(root, ".next/standalone/server.js");
if (!existsSync(server)) throw new Error("请先执行 npm run build");
cpSync(path.join(root, "public"), path.join(root, ".next/standalone/public"), { recursive: true });
cpSync(path.join(root, ".next/static"), path.join(root, ".next/standalone/.next/static"), { recursive: true });
const child = spawn(process.execPath, [server], {
  cwd: root, stdio: "inherit",
  env: { ...process.env, NODE_ENV: "production", HOSTNAME: "127.0.0.1", PORT: process.env.PORT || "3030" },
});
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill(signal));
child.on("exit", code => process.exit(code ?? 0));
