# Mock 封版验证记录

验证日期：2026-10-26。范围：已有人工复核的版本关联、内容检查产品接入、核心创作流程。全程保持 `MODE_MOCK=1`、`DISABLE_OUTBOUND_NETWORK=1`，没有调用真实模型或产生模型费用；没有部署生产或云端复验。

## 人评和离线检查

- R07、H01、R10：3/3 版本绑定核验通过；只新增关联，9 项原判断及理由不变。R10 仅适用原创作请求语境。
- 52 条离线回放：与当前 v13 已保存结果逐条一致；44 条 NOT_CHECKED、3 条 TEXT_EVIDENCE_FOUND、5 条 NEED_REVIEW。
- H05 仍为 NEED_REVIEW，没有放宽其无依据防护因果检查。原人工评分、历史答复及冻结检查器均未改写。
- 原人工评分记录 SHA-256：`27bed312faf64927843391171f189316832f1a4cdb1d36cb8f53ba2624c62c04`。

## 自动验证

- 后端本轮相关 Mock 回归：**2879 passed，2 warnings**。包含检查记录、精确版本关联、API 权限、工作流与 Agent 模式的模拟失败恢复、阶段版本、人工确认流程，以及冻结检查器基础和 v2–v6、v8–v13 回归。
- 前端：**98 项测试通过**；TypeScript 检查及生产构建通过。
- 额外从 Git 暂存区导出独立目录，运行检查接入、版本关联、Mock 恢复、离线隔离和 v13 回归：**354 项通过**，确认不依赖工作区未提交的其他材料。
- 新增 Mock 恢复 API 测试覆盖：恢复后继续、内容检查记录、越权拒绝、不支持阶段拒绝，以及未标记／真实执行不能借用 Mock 恢复路径。

后端复现命令（项目根目录，Python 依赖已安装）：

```bash
MODE_MOCK=1 DISABLE_OUTBOUND_NETWORK=1 TRIAL_MODE_ENABLED=false PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 backend/.venv/bin/python -m pytest -p pytest_asyncio.plugin backend/tests/test_creation_checks.py backend/tests/test_review_version_links.py backend/tests/test_mock_release_api.py backend/tests/test_orchestrator.py backend/tests/test_comic_workflow.py backend/tests/test_api.py backend/tests/test_auth.py backend/tests/test_story_versions.py backend/tests/test_execution_recovery.py backend/tests/test_review_manual_flow.py backend/tests/test_offline_mock.py backend/tests/test_answer_grounding.py backend/tests/test_answer_grounding_v2.py backend/tests/test_answer_grounding_v3.py backend/tests/test_answer_grounding_v4.py backend/tests/test_answer_grounding_v5.py backend/tests/test_answer_grounding_v6.py backend/tests/test_answer_grounding_v8.py backend/tests/test_answer_grounding_v9.py backend/tests/test_answer_grounding_v10.py backend/tests/test_answer_grounding_v11.py backend/tests/test_answer_grounding_v12.py backend/tests/test_answer_grounding_v13.py -q --disable-warnings
```

验证边界：另一次尝试运行旧 `test_recovery_api.py` 套件时有 15 项未通过；抽查确认存在依赖模拟 HTTP 传输被调用的测试，而强制 Mock 未进入该路径。本轮没有关闭 Mock 去满足这些传输测试，也没有宣称全仓库测试全通过。真实提供方恢复契约仍待后续复验；本轮两种编排模式的本地失败恢复已由新增测试和界面实测覆盖。

## 浏览器核心流程

使用隔离演示库，在 `127.0.0.1:3030` 实际点击，后端仅监听 `127.0.0.1:8030`：

1. 创建故事短视频并生成剧本：检查器自动展示无依据保证、所在剧情及修改建议。
2. 编辑并保存：重新检查，历史由 1 次增加为 2 次；切回生成版本后恢复原提示，刷新页面仍保留历史。
3. 确认剧本后生成角色和场景图；修改角色提示词并只重生成一张：角色图片地址更新，场景图片地址不变。
4. 逐阶段确认并生成分镜、参考图、视频、成片；成片可播放，媒体 readyState 为 4，时长约 15.02 秒；确认归档成功。
5. 新建恢复演示，触发有意的本地中断：出现明确错误和“继续未完成阶段”；点击继续后生成成功、进入等待确认。刷新后状态保持，页面错误提示为空。
6. 成片和恢复后页面均检查了截图，未发现白屏或布局缺失。上述核心流程除有意模拟中断外，没有页面报错。

演示素材与故障为本地固定夹具，不计入模型效果评分、独立留出集或真人双评。
