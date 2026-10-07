# AI造梦机 · RAG 知识结构设计表

> 更新：2026-10-06  
> 性质：索引页。权威原文是 [RAG技术与数据设计](RAG技术与数据设计.md)「数据合同」「选型与运行」两节，本表只做摘录，两者不一致时以原文为准。  
> 代码依据：`backend/app/services/knowledge_store.py`、`backend/app/services/knowledge_retrieval.py`

## 1. 数据对象

| 对象 | 核心字段 | 规则 |
| --- | --- | --- |
| Library（资料库） | `library_id, owner_id, name, description, revision` | 改资料会推进 revision；所有接口先校验 owner |
| Document（资料） | `document_id, library_id, current_version_id` | 当前有效版本决定检索范围；删除后退出新检索 |
| Version（版本） | `version_id, version_number, title, category, is_constraint, text, chunks` | 原文和切片不可变；替换文件或改元信息产生新版本 |
| Chunk（切片） | `chunk_id, text` | 按段落 / 标题切分，长段 380 字、60 字重叠，单版本最多 800 片 |
| Retrieval（检索快照） | `library_ids, library_versions, query, sources, status, warnings, embedding_model` | 先存入 execution_inputs，再调用模型 |
| Source（来源） | `citation_id, document_id, version_id, title, text, score, is_constraint` | `K1…` 为本次上下文编号；固定约束无相似度分数 |

## 2. 分类与资料类型

| 维度 | 取值 / 规则 |
| --- | --- |
| 分类 | 世界观 / 角色 / 剧情 / 风格 / 品牌 / 其他 |
| 固定约束 | `is_constraint=true`，整份注入，不参与相似度过滤；单库及本次绑定库合计 ≤ 6000 字，超限报错不裁剪 |
| 普通资料 | 切片后向量检索，默认 Top 5（接口允许 1–10） |
| 文件格式 | `.md` / `.txt` / 可提取文字的 PDF，单文件 5MB，解析文本 ≤ 20 万字符；扫描件不做 OCR |

## 3. 存储分层

| 层 | 实现 | 职责 |
| --- | --- | --- |
| 权威记录 | SQLite `data/knowledge/knowledge.db` | 原文、版本、归属、库 revision |
| 向量索引 | Qdrant 本地磁盘模式 `data/knowledge/vectors`，集合 `creative_knowledge_bge_zh_v1` | 只做检索；按 owner、library、有效 version 三重过滤 |
| 向量模型 | FastEmbed + `BAAI/bge-small-zh-v1.5`，512 维，余弦相似度 | 缓存于 `data/knowledge/models` |
| 生成快照 | 剧本 / 分镜产物、阶段版本、execution_inputs | 保存同一份来源快照，资料变更后提示而不自动重生成 |
