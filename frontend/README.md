# AI造梦机 · 前端

从一句创意到一部完整成片的 AI 短片/短剧创作系统的正式前端。Next.js（App Router）+ React 19 + TypeScript strict + Tailwind CSS 4，开发端口 **3030**。

## 页面

- **登录页** `/login`：邀请码登录
- **工作台** `/`：左侧导航（创作 / 短管线 / 设置 / 沙盒）
  - **创作**：新建会话 → 6 阶段主流程（剧本 → 角色/场景 → 分镜 → 参考图 → 视频 → 成片），逐阶段确认/重做，下载成片
  - **短管线**：文艺短视频 / 动作迁移 / 数字人口播，提交后后台生成，任务列表看进度与下载
  - **设置**：只读展示当前模型配置与内容安全开关（后端 `GET /api/settings`）
  - **沙盒**：后端健康检查 + 会话/任务统计

## 启动

```bash
cd frontend
npm install        # 首次
npm run dev        # 打开 http://127.0.0.1:3030
```

需后端已运行在 8030；登录接口、会话/任务/产物媒体、`/api/settings` 均通过 Next `rewrites` 代理到后端（见 `next.config.ts`）。

## 检查

```bash
npm run typecheck
npm run lint
npm run build
```

## 结构

| 目录 | 内容 |
| --- | --- |
| `app/` | 入口、全局样式、登录页 |
| `features/auth/` | AuthProvider（令牌存 localStorage） |
| `features/workspace/` | 工作台布局 + 侧栏导航 |
| `features/creation/` | 创作表单、剧本展示、六阶段产物展示、媒体组件 |
| `features/pipelines/` | 三条短管线表单 + 任务列表 |
| `features/settings/` `features/sandbox/` | 设置页 / 沙盒页 |
| `lib/api/` | 集中式 API 客户端（auth/sessions/tasks/settings） |
| `lib/` | 流式 SSE、媒体下载、配置 |

## 已知边界

- 鉴权用 `Authorization: Bearer` + `localStorage`（SSE 需自定义头），生产上线前迁 HttpOnly Cookie。
- 产物图片/视频用 blob ObjectURL 加载，不走 next/image 优化。
- 真实生成速度受模型商延迟影响（视频片段约 1~2 分钟/段），前端以「流程可用」为准。
