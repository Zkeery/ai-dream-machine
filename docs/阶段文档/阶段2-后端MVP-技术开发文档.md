# 阶段 2 技术开发文档｜后端 MVP（第一刀）

> 配套文档：PRD V1.0、《技术适配声明》《架构方案》、通用技术栈手册。
> 本文档只覆盖阶段 2（第一刀：6 阶段主流程，本地单机）。短管线、登录/多人、数据库均为后续刀，本阶段不得提前实现。

---

## 一、阶段目标

- **交付范围**：6 阶段主流程后端 + 最小验收界面。用户输入一句创意 → 剧本 → 角色/场景 → 分镜 → 参考图 → 视频片段 → 自动剪辑成片，每阶段可确认/修改/重生成，产物本地留存并可导出。
- **阶段产物**：可运行的后端（FastAPI）+ 最小验收界面 + mock 测试 + 产物目录。
- **验收标准**（对齐 PRD 4.1）：
  1. 从一句创意走完 6 阶段到成片，中途不卡死
  2. 每阶段产物可查看、可确认、可修改后重生成
  3. 成片产物落盘并可导出
  4. 无真实 Key 时 mock 测试全过；真实冒烟如实标注「待验」
- **明确不做**：3 条短管线、邀请码登录/账号隔离、数据库（多实体）、剧本智能续写、跨镜头一致性 Agent。
- **本阶段打通的主链路**：创意录入 → 6 阶段状态机 → 各阶段模型调用 → 产物落盘 → SSE 进度 → 成片导出。

## 二、技术适配摘要

- 引用《技术适配声明》：开发路径为**纵向切片**；本地文件存储；暂缓数据库/短管线/登录。
- 本阶段采用：Python 3.12 + FastAPI + Pydantic；pytest；Git + `.env`。
- 本阶段启用按需模块：本地文件目录（图片/视频资产）、后台任务 + SSE（视频慢）、FFmpeg（成片）。
- 本阶段偏离/暂缓：SQLite → 带 schema 版本的文件清单（单用户、资产以文件为主）；登录 → 无（本地单机）。
- 开发路径：纵向切片（后端 + 最小验收界面一起做）。

## 三、技术栈与模型

- 后端：Python 3.12、FastAPI、Pydantic v2、httpx（模型调用）、edge-tts（配音）、FFmpeg（系统级，已装 9.0.2）。
- 存储：本地文件目录 + JSON（schema 版本 v1）。
- 测试：pytest + pytest-asyncio。
- 模型选型（国内，可插拔，均从 `.env` 读配置）：

| 用途 | 厂商 | 模型名 | 环境变量 |
| --- | --- | --- | --- |
| LLM（剧本/分镜/提示词） | 通义 DashScope | qwen3.5-plus | `DASHSCOPE_API_KEY` |
| VLM（视觉描述/审查） | 通义 DashScope | qwen3.5-plus | 同上 |
| 文生图 / 图生图 | 火山 ARK | doubao-seedream-5-0-260128 | `ARK_API_KEY` |
| 首帧生视频 / 首尾帧 | 通义 DashScope | wan2.7-i2v | `DASHSCOPE_API_KEY` |
| 参考图生视频 | 通义 DashScope | wan2.7-r2v | `DASHSCOPE_API_KEY` |
| TTS 配音 | 本地 Edge TTS | 默认 zh-CN-YunjianNeural | 无 |

- 不引入：SQLAlchemy/Alembic（无数据库）、Redis/队列（单机后台线程足够）、RAG。

## 四、环境与配置

- 配置项：`.env`（不入库），`.env.example`（提交，只含占位）。字段见下。
- 需要产品经理提供：`DASHSCOPE_API_KEY`（通义）、`ARK_API_KEY`（火山）。**验收前提供即可，不阻塞开发**（开发期用 mock）。
- 端口：后端 **8030**；最小验收界面由后端托管（`/`），不另起前端。
- 外部依赖：FFmpeg（已装）。

```bash
# .env.example
DASHSCOPE_API_KEY=
ARK_API_KEY=
# 可选覆盖（默认值已内置，一般不用改）
LLM_MODEL=qwen3.5-plus
IMAGE_T2I_MODEL=doubao-seedream-5-0-260128
VIDEO_FIRST_FRAME_MODEL=wan2.7-i2v
VIDEO_REFERENCE_MODEL=wan2.7-r2v
```

## 五、项目结构

```text
backend/
├── app/
│   ├── api/            # 路由：sessions.py / files.py / health.py
│   ├── core/           # 配置、日志、安全（path_security）、错误模型
│   ├── schemas/        # Pydantic 输入输出结构
│   ├── services/       # orchestrator（状态机）、6 个阶段 agent、pipeline（本阶段不用）
│   │   └── prompts/    # 各阶段 Prompt 模板（独立文件）
│   ├── models/         # 各家模型客户端封装（llm/vlm/image/video/tts）
│   ├── static/         # 最小验收界面（HTML+原生JS）
│   └── main.py         # FastAPI 入口
├── tests/              # mock 测试
├── data/sessions/      # 会话元数据 JSON（运行时生成，gitignore）
├── result/             # image/ video/ script/ 产物目录（运行时生成，gitignore）
└── requirements.txt
```

## 六、数据、资产与状态

- **持久化方式**：结构化文件（JSON，schema 版本 `v1`）+ 本地文件目录。单用户、资产以图片/视频文件为主，无复杂查询，符合暂缓数据库的裁决。
- **会话元数据** `data/sessions/{session_id}.json`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| session_id | string | 会话唯一 ID（毫秒时间戳） |
| idea | string | 用户原始创意 |
| style / video_ratio / resolution / episodes | string/int | 生成参数 |
| current_stage | string | 当前阶段 |
| status | string | 状态（见状态机） |
| stages_completed | string[] | 已完成阶段 |
| artifacts | object | 各阶段产物摘要（路径、版本、选择） |
| created_at / updated_at | float | 时间戳 |

- **资产目录**：`result/image/{session}/`（角色/场景/参考图）、`result/video/{session}/`（片段/成片）、`result/script/{session}/`（剧本/分镜 JSON）。
- **写入规则**：原子写入（先写临时文件再 rename）；损坏文件处理＝标记失败可重跑，不崩溃。
- **状态机**：

```text
idle → running → waiting（等用户确认/修改）→ stage_completed →（下一阶段）→ … → session_completed
任一阶段可 → failed（错误可读，可重试）
```

```text
阶段顺序：script_generation → character_design → storyboard
         → reference_generation → video_generation → post_production
```

- 关键人工确认点（剧本定稿、续写合并、删除）持久化到会话 JSON，刷新/重启后可恢复继续。

## 七、API 设计

统一前缀 `/api`，错误统一 `{"error":{"code","message"}}`，不泄露堆栈。

| 方法 | 路径 | 说明 | 长耗时 |
| --- | --- | --- | --- |
| POST | `/api/sessions` | 创建会话（idea + 参数） | 否 |
| GET | `/api/sessions/{id}` | 查询会话状态 | 否 |
| POST | `/api/sessions/{id}/execute/{stage}` | 执行指定阶段 | SSE |
| POST | `/api/sessions/{id}/continue` | 确认当前阶段、进入下一阶段 | 否 |
| POST | `/api/sessions/{id}/intervene` | 修改产物后重生成 | SSE |
| GET | `/api/sessions/{id}/artifact/{stage}` | 获取阶段产物 | 否 |
| GET | `/api/sessions/{id}/events` | 订阅进度 | SSE |
| GET | `/api/sessions/{id}/export` | 导出成片/产物 | 否 |
| POST | `/api/upload` | 上传素材（本阶段仅预留，主流程不依赖） | 否 |
| GET | `/api/health` | 健康检查 | 否 |

- **SSE 事件序列**：`progress`（零到多次，含百分比/阶段）→ `stage_complete` → 最终以 `done`（含产物摘要）或 `error`（统一错误结构）收尾。
- **关键接口示例**：
  - 创建会话 `POST /api/sessions`，请求 `{"idea":"...", "style":"realistic", "episodes":4, "video_ratio":"16:9"}`，响应 `{"session_id":"...", "current_stage":"script_generation"}`。
  - 执行剧本 `POST /api/sessions/{id}/execute/script_generation`，SSE 推送进度，完成后 `done` 事件含 `artifact`（title/logline/characters/settings/episodes）。
  - 修改角色后重生成：`POST /api/sessions/{id}/intervene`，`{"stage":"character_design","modifications":{...}}`。
- **校验规则**：`idea` 非空、长度上限 2000；`episodes` 1~20；`video_ratio` 枚举 16:9/9:16/1:1；上传校验格式/真实类型/大小/安全文件名。

## 八、Prompt 设计

每份 Prompt 独立文件（`services/prompts/`），角色 + 输入变量 + 输出格式 + 约束。

- **剧本**：输入 idea/style/episodes；输出 JSON（title/logline/genre/mood/characters[]/settings[]/episodes[]）；约束：可拍摄、含画面/动作/对白；禁止真人肖像/侵权 IP/违法内容。格式三件套：给正反例、解析宽容、数量超限记为瑕疵不重试。
- **角色/场景**：输入剧本角色列表；输出视觉提示词 + 生成多版本图。
- **分镜**：输入剧本；输出镜头列表（shot_id/画面/提示词）。
- **参考图**：输入分镜提示词；输出首帧图。
- **视频**：输入首帧图/参考图 + 提示词 + 生成方式；输出视频片段。
- **剪辑**：输入片段清单 + 分镜顺序；输出成片（FFmpeg 拼接 + 换配音 + 字幕）。

模型输出统一「模型输出 → 后端确定性解析 → Pydantic 强校验」，校验失败有限重试（2 次），仍失败走统一错误。

## 九、验收界面（最小纵向切片）

- 本阶段做**最小临时验收界面**（后端 `static/` 原生 HTML+JS，非正式产品前端，正式前端在阶段 3）。
- 功能上限：输入创意 → 逐阶段「生成/查看产物/确认继续/修改重生成」→ 查看成片 → 下载导出。
- 目标终端：桌面浏览器为主，移动端仅保证可看。

## 十、测试要求

### 第一层 mock 测试（离线，模型全 mock）
1. 状态机流转：idle→…→session_completed、failed 可重试
2. 剧本/分镜/角色输出解析器：正常 JSON、缺失字段、错误格式、中文序号兼容（纯函数单测）
3. API 参数校验：idea 空/超长、episodes 越界、video_ratio 非法
4. 错误路径：模型 mock 抛错 → 统一错误结构、有限重试
5. 文件安全：上传路径遍历、伪造扩展名、超大小
6. 数据恢复：写会话 JSON → 模拟重启（重建进程读文件）→ 状态可继续

### 第二层真实冒烟（有 Key 后）
- 用一句真实创意跑通完整 6 阶段链路，记录模型、每阶段耗时、成本。
- 用 `curl -N` 计时验证 SSE 首事件时间与 done/error 收尾。
- 跑 5~10 条真实样例供产品经理打分（定 D7 验收标准，维度：符合描述/质量可用/风格一致/无违规内容）。
- 无 Key 时如实标注「待验」，不用 mock 冒充。

## 十一、验收清单（产品经理照着点）

1. 终端执行 `cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt`（AI 代跑）
2. 把通义 Key、火山 Key 填进 `backend/.env`（AI 指引位置，不回显）
3. 启动后端（AI 代跑），浏览器打开 `http://localhost:8030`
4. 输入框写一句创意（如「一只流浪猫在雨夜被好心人收留」），选风格、剧集数，点「开始」
5. 看到剧本生成：确认标题、梗概、角色、剧集正文 → 点「确认继续」
6. 逐阶段看到角色图、分镜、参考图、视频片段，可改可重生成
7. 最后看到「成片」可播放，点「下载」拿到视频文件
8. 重启后端，确认之前进度还在、可继续

## 十二、风险与待确认项

- 视频生成 1~2 分钟/片段，首版单机串行，长片耗时长（已知限制，后续并发增强）。
- 真实模型成本未实测，接近 500 元/月上限时需提醒产品经理。
- 内容红线「生成结果侧审查」用 VLM 粗筛，非法律级审核（对外上线前再强化）。
- 必须由产品经理决定的问题：**无**（D7 验收标准待第一次冒烟打分，不阻塞开发）。

## 十三、交接给下一阶段

- 已就绪：6 阶段后端 API、状态机、模型客户端（可插拔）、产物落盘/导出、mock 测试。
- 下一阶段（第二刀短管线 / 阶段 3 正式前端）直接复用：模型客户端、会话状态机、产物目录约定、SSE 约定。
