#!/bin/bash
# AI造梦机 · 本地自用一键启动（后端 8030 + 前端 3030）
# 用法：在终端执行  ./start.sh  （或 bash start.sh）
set -e
cd "$(dirname "$0")"

if [ ! -d backend/.venv ]; then
  echo "❌ 后端虚拟环境不存在，请先执行："
  echo "   cd backend && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
if [ ! -d frontend/node_modules ]; then
  echo "❌ 前端依赖未安装，请先执行："
  echo "   cd frontend && npm install"
  exit 1
fi

echo "🚀 启动 AI造梦机："
echo "   前端  http://127.0.0.1:3030"
echo "   后端  http://127.0.0.1:8030"
echo "   （按 Ctrl+C 可同时停止两个服务）"
echo

# Ctrl+C 时同时停掉前后端
trap 'echo; echo "已停止。"; kill 0' EXIT INT TERM

(cd backend && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8030) &
(cd frontend && exec npm run dev) &

wait
