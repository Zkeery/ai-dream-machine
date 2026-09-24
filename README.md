# AI造梦机

从一句创意到一部完整成片的 AI 短片 / 短剧创作系统。6 阶段主流程（剧本 → 角色/场景 → 分镜 → 参考图 → 视频 → 成片）+ 3 条短管线（文艺短视频 / 动作迁移 / 数字人口播）+ 邀请码登录与账号隔离。

## 当前状态

- 后端：阶段 2 MVP 四刀完成（6 阶段主流程 + 短管线 + 邀请码登录/账号隔离 + SQLite 数据库）
- 前端：阶段 3 子阶段 1~4b 完成（Next.js：登录/工作台/六阶段/短管线/任务中心/设置页/沙盒页 + 视觉升级）
- 进度与决策见 [项目状态](docs/项目状态.md)

## 文件位置

| 内容 | 位置 |
| --- | --- |
| 项目身份、开发端口 | `project.json`（后端 8030 / 前端 3030） |
| 需求 | `docs/PRD/` |
| 技术方案与开发文档 | `docs/阶段文档/` |
| 验收证据 | `docs/evidence/` |
| 后端代码与测试 | `backend/` |
| 前端代码 | `frontend/` |
| 运行数据（不入 Git） | `data/`（SQLite + 图片/视频/剧本产物） |

## 启动

### 一键启动（推荐，本地自用）

```bash
cd ~/Documents/happy/项目开发必备文档/projects/AI造梦机
./start.sh
```

同时起后端 8030 + 前端 3030，按 Ctrl+C 一起停。打开 http://127.0.0.1:3030 使用。

### 环境要求
- Python 3.12.x、Node.js 18+、ffmpeg（已装 9.0.2）

### 后端（8030）

```bash
cd backend
python3.12 -m venv .venv                        # 首次
.venv/bin/pip install -r requirements.txt       # 首次
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8030
```

### 前端（3030）

```bash
cd frontend
npm install        # 首次
npm run dev        # 打开 http://127.0.0.1:3030
```

### 密钥（真实生成前提供）

在 `AI造梦机/.env` 填 `AIHUBMIX_API_KEY`（网关 AIHubMix，一个 Key 调通文本/图片/视频）。无 Key 不阻塞开发，mock 测试离线可跑。

### 邀请码（登录用）

```bash
cd backend
.venv/bin/python scripts/manage_invite_codes.py generate -n 3   # 生成邀请码
.venv/bin/python scripts/manage_invite_codes.py list            # 查看
```

## 测试

```bash
# 后端（57 项）
cd backend && .venv/bin/python -m pytest tests/ -q

# 前端
cd frontend && npm run typecheck && npm run lint && npm run build
```

## 验收路径

1. 打开 3030 → 邀请码登录 → 工作台
2. 「创作」：输入创意 → 开始创作 → 逐阶段生成（剧本→角色图→分镜→参考图→视频→成片），每阶段可确认/重做，最后下载成片
3. 「短管线」：文艺短视频 / 动作迁移 / 数字人口播，提交后后台生成，任务中心看进度与下载
4. 两个账号登录，验证只能看各自的数据（账号隔离）

## 边界与已知问题

- 内容红线：禁止生成真人肖像、侵权 IP、违法内容（提示词 + 结果双向拦截）。
- 模型网关：AIHubMix，文本 qwen3.5-plus / 图片 qwen-image-2.0 / 视频 wan2.7-i2v·r2v。
- 动作迁移为「角色图 + 提示词」生成，非像素级动作迁移；数字人口播为静态图 + 配音，无嘴型同步（如实标注）。
- 真实生成速度受模型商延迟影响，视频片段约 1~2 分钟/段。
- 并发分镜优化已暂缓。
