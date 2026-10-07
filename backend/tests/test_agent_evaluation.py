"""The paid evaluator itself is verified with local transports only."""
import importlib.util
import json
from pathlib import Path

import pytest

from app.core.errors import AppError


@pytest.fixture
def evaluator():
    path = Path(__file__).parents[1] / "scripts/evaluate_agent_reliability.py"
    spec = importlib.util.spec_from_file_location("agent_evaluation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_never_imports_clients_or_requests_network(evaluator, monkeypatch, tmp_path):
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("dry run attempted network")

    monkeypatch.setattr(httpx, "post", forbidden)
    output = tmp_path / "plan"
    monkeypatch.setattr("sys.argv", ["evaluate", "--output", str(output)])
    evaluator.main()
    plan = json.loads((output / "manifest.json").read_text())
    assert plan["repeats"] * len(plan["cases"]) == 6
    assert plan["max_image_calls"] == 1
    assert plan["max_estimated_cny"] == 6
    assert not (output / "data").exists()


def test_batch_guard_counts_failed_attempts_and_stops_before_request(evaluator):
    from types import SimpleNamespace
    calls = []
    cost = SimpleNamespace(reserve=lambda *args, **kwargs: calls.append(args),
                           _rate=lambda *args: {}, _estimate=lambda *args: 1_000_000,
                           _money=lambda amount: amount * 1_000_000)
    budget = evaluator.install_budget_guard(cost, AppError, limit=2)
    cost.reserve("one", "text-model", "text", {})
    cost.reserve("two", "text-model", "text", {})
    with pytest.raises(AppError) as exc:
        cost.reserve("three", "text-model", "text", {})
    assert exc.value.code == "EVAL_BUDGET_LIMIT"
    assert len(calls) == len(budget["calls"]) == 2


@pytest.mark.asyncio
async def test_live_runner_smoke_with_offline_clients(evaluator, data_dirs, monkeypatch, tmp_path):
    from app.core import config
    from app.services import cost_control, orchestrator
    from tests.test_agent_runtime import Decisions
    from PIL import Image

    output = tmp_path / "batch"
    output.mkdir()
    monkeypatch.setenv("DATA_DIR", str(output / "data"))
    for attr in ("DATA_DIR", "SESSIONS_DIR", "IMAGE_DIR", "VIDEO_DIR", "SCRIPT_DIR", "UPLOAD_DIR", "RESULT_DIR"):
        monkeypatch.setattr(config, attr, output / "data" / attr.lower() if attr != "DATA_DIR" else output / "data")
    original_init = orchestrator.Orchestrator.__init__

    class OfflineLLM:
        def generate_json(self, system, user, schema, **kwargs):
            from app.services.agent_runtime import AgentAction, PlannerIntent
            if schema is PlannerIntent:
                self.decisions = Decisions()
            return self.decisions.generate_json(system, user, schema, **kwargs)

    class OfflineImage:
        def text_to_image(self, prompt, path, **kwargs):
            Image.new("RGB", (16, 16), "white").save(path)

    def init(self):
        original_init(self, llm=OfflineLLM(), image=OfflineImage())

    monkeypatch.setattr(orchestrator.Orchestrator, "__init__", init)
    # Restore the module-global wrapper after the batch.
    monkeypatch.setattr(cost_control, "reserve", cost_control.reserve)
    await evaluator.live(output, evaluator.manifest())
    report = json.loads((output / "report.json").read_text())
    assert len(report["cases"]) == 6
    assert all(case["passed"] for case in report["cases"])
    assert report["image"]["execution"]["status"] == "completed"
    assert report["image_visual_review"] == "pending"
    assert report["verdict"] == "requires_visual_review"
