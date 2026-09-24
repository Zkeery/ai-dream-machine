# 阶段 2 技术开发文档｜第四刀：数据持久化迁移到 SQLite

> 配套文档：PRD V1.0、《技术适配声明》（原计划"第三刀做多人/账号隔离时迁移数据库"）、《架构方案》、前三刀手册。
> 本文档只覆盖第四刀（把元数据从 JSON 文件迁到 SQLite）。图片/视频/剧本等大产物仍以文件存储，不进数据库。

---

## 一、目标与范围

- **目标**：把会频繁增删改查的**元数据**（邀请码、用户、令牌、上传索引、会话、任务）从 JSON 文件迁移到本地 SQLite，为对外多人、并发读写、列表查询打基础。
- **范围**：
  - 新建 SQLite 数据库 `data/app.db`（gitignore 已排除）。
  - 6 张表：`invite_codes`、`users`、`auth_tokens`、`uploads`、`sessions`、`tasks`。
  - 复杂字段（会话产物、任务输入/结果、stages_completed 等）以 JSON 文本列存储。
  - **图片/视频/剧本等大产物继续存文件**，DB 只存路径与元数据。
  - 提供一次性迁移脚本（JSON → SQLite），迁移后 JSON 保留为备份，可回滚。
- **不做**：SQLAlchemy/ORM、Redis、多数据库、复杂索引/全文检索、并发分镜。

## 二、技术选型

- **SQLite + 标准库 `sqlite3`**（不引入 SQLAlchemy，避免多余依赖）。
- 并发：SQLite WAL 模式 + 每次操作独立连接（`contextmanager`），天然线程安全（模型调用在 `asyncio.to_thread` 里）。
- 与现有 Pydantic 模型保持兼容：`SessionMeta`/`TaskMeta` 的 `model_dump()` / `model_validate()` 不变，只是存储介质从文件换成表。

## 三、表结构

| 表 | 主键 | 关键列 |
| --- | --- | --- |
| `invite_codes` | code | status(unused/used/revoked), used_by, created_at, used_at |
| `users` | user_id | invite_code, created_at |
| `auth_tokens` | token | user_id, created_at, expires_at |
| `uploads` | filename | owner_id, original_name, created_at |
| `sessions` | session_id | owner_id, idea, status, current_stage, stages_completed(JSON), artifacts(JSON), error, created_at, updated_at, … |
| `tasks` | task_id | type, owner_id, status, input(JSON), result(JSON), error, created_at, updated_at |

- 布尔值（如 `expand_idea`）存 0/1。
- JSON 列用 `json.dumps` / `json.loads` 存取；损坏时返回可读错误。

## 四、实现要点

1. 新建 `app/services/db.py`：
   - `db_path()`（跟随 `config.DATA_DIR`，测试可隔离）
   - `connect()` 上下文管理器（独立连接 + 提交）
   - `init_db()` 建表（幂等 `CREATE TABLE IF NOT EXISTS`）
   - 迁移后字段与 JSON 版本字段对齐，避免语义漂移。
2. 重构三处存储层：
   - `auth.py`：`_read/_write` JSON 改为 SQL 增删查改。
   - `session_store.py`：`create/save/load/list` 改为 SQL。
   - `task_store.py`：同样改为 SQL。
3. 启动时 `init_db()`（`main.py` 调用）。
4. 迁移脚本 `scripts/migrate_to_sqlite.py`：
   - 读取旧 JSON → 逐条 INSERT → 校验行数一致 → 旧 JSON 移到 `data/backup_json/`（不删除）。
   - 空库直接 init；已有 DB 不重复迁移。

## 五、测试计划（mock，不调真实模型）

| 类别 | 用例 |
| --- | --- |
| 建库 | 首次 init 建 6 表；重复 init 幂等 |
| 会话 | create/load/list/更新 status 落库；重启可恢复；owner 过滤 |
| 任务 | create/status 更新/result 落库；list 过滤 |
| 鉴权 | 邀请码/用户/令牌/上传的增删查改与隔离校验不变 |
| 迁移 | 旧 JSON 全部迁入 DB、行数一致、产物文件不变；旧 JSON 备份不删除 |
| 回归 | 前三刀 mock 测试（54 项）全部通过；API 行为不变 |

## 六、验收清单

- [ ] 6 表建成，幂等
- [ ] 会话/任务/鉴权读写全部走 DB，行为与 JSON 版一致
- [ ] 迁移脚本跑通，行数一致，旧 JSON 备份保留
- [ ] 服务重启后数据可恢复（DB）
- [ ] 全量测试通过；密钥未入库
- [ ] 状态文件与证据包更新
