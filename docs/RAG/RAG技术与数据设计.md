# V1.2 创作知识库：技术与数据设计

更新：2026-10-06。V1.2 的知识库设计在 V1.3 扩展为故事 / 漫剧共用；需求入口为 [PRD 第 10、12 节](../PRD/PRD.md)，验收与限制见 [RAG 验收记录](../evidence/RAG/验收记录.md)。

## 模块关系

![知识库、故事项目与模型的关系](../PRD/diagrams/rag-architecture.svg)

上图保留 V1.2 故事链路结构。当前知识库供故事 / 漫剧项目各自选择最多三个库，在剧本和分镜 / 镜头对白阶段检索并固定本次来源；两条链路分别管理下游产物与依赖。共用资料库不合并项目版本，也不开放其他账号资料。三种快捷短片暂不接入 RAG。图片素材库仍负责图片引用，不承担文本检索。

## 选型与运行

| 部分 | 本版实现 | 原因与边界 |
| --- | --- | --- |
| 文本向量 | FastEmbed + `BAAI/bge-small-zh-v1.5`，512 维 | 中文资料、本地 CPU；首次下载约 90MB 模型，后续使用缓存 |
| 向量存储 | Qdrant Python client 的本地磁盘模式 | 保持前端 3030、后端 8030 两个服务，无 Docker 服务；仅运行一个后端 worker |
| 资料与版本 | `data/knowledge/knowledge.db` SQLite | 原文、版本、归属与库 revision 是权威记录；向量只做检索 |
| 生成模型 | 用户所选的兼容 LLM、既有网关与 JSON 校验 | 来源快照与 `model_usage` 分开保留；不改变本地 Embedding |
| 解析 | UTF-8 文本与 pypdf | `.md/.txt/.pdf`，5MB；PDF 扫描件明确不支持 OCR |

官方依据：[FastEmbed 模型表](https://github.com/qdrant/fastembed/blob/main/fastembed/text/onnx_embedding.py)、[Qdrant 本地模式](https://github.com/qdrant/qdrant-client#local-mode)。本版没有进行多个 Embedding 模型的效果排名，也没有独立 Reranker。

安装依赖：在 `backend/` 执行 `.venv/bin/pip install -r requirements.txt`。默认模型缓存 `data/knowledge/models`，可用 `KNOWLEDGE_MODEL_CACHE` 指向共享缓存；向量目录 `data/knowledge/vectors`。程序按需初始化模型，启动旧项目或查看页面不必先加载模型。

**必须使用单 worker**：`uvicorn app.main:app --host 127.0.0.1 --port 8030`。多进程同时访问 Qdrant 本地目录会报索引不可用；需要多 worker 时先迁移至 Qdrant 服务端，再做并发验收。

## 数据合同

| 对象 | 核心字段 | 规则 |
| --- | --- | --- |
| Library | `library_id, owner_id, name, description, revision` | 修改资料会推进 revision；所有接口先校验 owner |
| Document | `document_id, library_id, current_version_id` | 当前有效版本决定检索范围；删除后退出新检索 |
| Version | `version_id, version_number, title, category, is_constraint, text, chunks` | 原文和切片不可变；替换文件或修改元信息产生新版本 |
| Chunk | `chunk_id, text` | 按段落切分，长段分片；380 字、60 字重叠，最多 800 片 |
| Retrieval | `library_ids, library_versions, query, sources, rejected_sources, status, warnings, adequacy, embedding_model, retrieval_mode, adequacy_enabled` | 本次检索快照，先保存到 execution_inputs，再进入模型；`status` 为 `matched` / `no_match` / `insufficient` |
| Source | `citation_id, document_id, version_id, title, text, score, is_constraint` | `K1…` 注入用编号；`rejected_sources` 用 `R1…`，表示已检索但未覆盖所问事实 |
| Adequacy | `sufficient, reason, unsupported_facts, degraded, error_code?, latency_ms?, model?` | 普通命中后的充分性判定；失败降级为不充分 |

分类：世界观 / 角色 / 剧情 / 风格 / 品牌 / 其他。解析文本最多 20 万字符；约束单库及本次绑定库合计最多 6000 字，超限报错，不能静默裁剪。普通结果默认取 Top 5，搜索接口允许 1–10 条；相似度阈值 0.48，总上下文预算 12000 字。初版 0.55 在开发集漏召回同义问题，保留失败结果后校准至 0.48；调参后开发集成绩不能冒充独立泛化能力，另补留出集。该阈值不代表普适正确性，后续以真实 Badcase 调整并保留评测批次。

## 生成与版本行为

1. 服务端校验会话归属和所选库归属，读取有效资料版本快照。
2. 所有固定约束进入上下文；普通资料按 owner、library、有效 version 同时过滤语义检索。
3. 保存检索快照后调用模型。资料以 JSON 数据注入用户消息，系统提示要求忽略资料中的指令；不能把文档内容提升成系统命令。
4. 剧本 / 分镜产物、阶段版本以及 execution_inputs 保存同一份来源快照。模型失败也保留已检索输入。
5. 用户修改正文时来源快照由服务端保留，并标记 `edited_since_generation`。来源面板只证明资料曾提供给模型，不证明每句正文引用正确。
6. 资料更新、删除或绑定库改变后，`knowledge_status` 提醒已有产物使用旧资料；不自动调用模型，也不强制推翻历史结果。用户主动重新生成后，已有 V1.1 下游失效逻辑才生效。

无匹配返回 `no_match`；只有固定约束时明确提示未命中其他资料。若开启充分性判定且普通命中不能支撑所问事实，返回 `insufficient`，普通命中进入 `rejected_sources`，warnings 说明未覆盖项；生成提示禁止据此编造姓名、价格等具体事实。判定失败或超时按不充分降级，不可静默当作充分。模型或向量索引故障返回 503，停止本次生成，不伪装成空结果继续。删除采用逻辑删除，历史资料版本与已有生成快照保留；当前没有提供彻底擦除历史引用的功能。

配置：`KNOWLEDGE_ADEQUACY_ENABLED`（默认开）、`KNOWLEDGE_ADEQUACY_MAX_RETRIES`；混合检索 `KNOWLEDGE_HYBRID_RETRIEVAL_ENABLED`（默认关，代码已具备，待误拒可控后再开）。

## API

统一前缀 `/api/knowledge`，需要现有登录令牌，错误沿用 `{error:{code,message}}`。

| 方法与路径 | 用途 |
| --- | --- |
| GET / POST `/libraries` | 列表 / 新建库 |
| PATCH / DELETE `/libraries/{id}` | 改名描述 / 移除库 |
| GET / POST `/libraries/{id}/documents` | 当前资料列表 / 上传文件（multipart） |
| GET / PATCH / DELETE `/documents/{id}` | 原文和版本摘要 / 元信息新版本 / 移除资料 |
| POST `/documents/{id}/versions` | 上传替换文件，产生新版本 |
| GET `/documents/{id}/versions/{version_id}` | 按归属查看指定历史原文 |
| POST `/libraries/{id}/search` | `{query,limit}` 本地检索测试 |
| PATCH `/api/sessions/{id}/knowledge` | `{knowledge_library_ids}`，生成中拒绝更改 |

`POST /api/sessions` 支持可选 `knowledge_library_ids`；旧请求默认空数组。`GET /api/sessions` 与详情增加 `knowledge_status`。SQLite 只增加新列，保留原来的会话数据。

## 验证与费用

工程测试用临时数据目录和 LLM 替身验证合同；独立检索评测使用真实本地中文 Embedding 与 Qdrant，记录逐题来源和耗时（0 次付费）。充分性拒答评测会调用既有文本模型，计入预算台账，证据见 `docs/evidence/RAG/rag-eval-refusal-*-20261006.json`。已有付费 LLM 对最终剧本事实一致性与引用质量的影响必须单列真实对照验收，不能由检索召回率推导。

资料上传与检索留在本地；用户选择知识库后主动生成或预览检索时，若开启拒答，会先对普通命中做一次轻量判定，再把可注入资料发至 AIHubMix。前端来源面板展示 `insufficient` 与被拒答资料；本版不新增云资源或计费体系。
