"""Local-only release demo: isolated data, offline clients, no real credentials.

The two named fixtures are synthetic demos, never model-quality evidence.
Run from the project root with backend/.venv/bin/python.
"""
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
for key in list(os.environ):
    if any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
        os.environ.pop(key, None)
os.environ.update(MODE_MOCK="1", DISABLE_OUTBOUND_NETWORK="1", SKIP_PROJECT_ENV="1",
                  DATA_DIR=str(ROOT / ".runtime/release-demo/data"), TRIAL_MODE_ENABLED="false", F1_ENABLED="0")
sys.path.insert(0, str(ROOT / "backend"))

from app.main import app
from app.api.deps import get_orchestrator
from app.core.errors import AppError
from app.models.llm_client import LLMClient
from app.services import db
from app.services.orchestrator import Orchestrator


class DemoLLM(LLMClient):
    failed = False

    def generate_json(self, system, user, model_cls, **kwargs):
        if "恢复演示" in user and model_cls.__name__ == "ScriptArtifact" and not self.failed:
            self.failed = True
            raise AppError("MOCK_DEMO_INTERRUPTED", "离线演示：模拟生成中断，已保留原输入，可以继续。", 503)
        result = super().generate_json(system, user, model_cls, **kwargs)
        if "内容检查演示" in user and model_cls.__name__ == "ScriptArtifact":
            result.title = "内容检查示例"
            result.episodes[0].content = "待在展示区就绝对安全，保证不会受到任何伤害。"
        return result


with db.connect() as conn:
    conn.execute("INSERT OR IGNORE INTO invite_codes (code, status, created_at) VALUES (?,?,?)",
                 ("DEM23456", "unused", time.time()))
get_orchestrator.cache_clear()
orchestrator = Orchestrator(llm=DemoLLM())
app.dependency_overrides[get_orchestrator] = lambda: orchestrator

if __name__ == "__main__":
    import uvicorn
    print("本地 Mock 演示：http://127.0.0.1:3030 · 邀请码 DEM23456 · 数据位于 .runtime/release-demo")
    uvicorn.run(app, host="127.0.0.1", port=8030, access_log=False)
