"""Frozen reliability batch. Default is a no-network plan; --live is explicit.

Control cases use a labelled local artifact fixture. The separate image probe
uses the actual provider. Neither is a full story/video quality evaluation.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
CASES = [
    {"id": "reported-character", "stage": "character_design", "idea": "大妈站在天台上吃炸鸡。"},
    {"id": "short-script", "stage": "script_generation", "idea": "雨夜，一只猫在便利店门口找到愿意收养它的人。单集故事。"},
    {"id": "constraint-character", "stage": "character_design", "idea": "一位成年女修理师穿蓝色工装在屋顶修天线。只有一名角色，不添加伙伴，不检索外部资料。"},
]
IMAGE_PROMPT = "写实电影风格，一位中年女性站在城市天台上吃炸鸡，半身人像，自然日光，人物手部清楚，背景简洁，无文字，无水印。"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def manifest():
    files = ["app/services/agent_runtime.py", "app/services/recovery.py", "app/services/provider_jobs.py",
             "app/models/llm_client.py", "app/models/image_client.py", "scripts/evaluate_agent_reliability.py",
             "model-cost-rates.json"]
    return {"version": 2, "cases": CASES, "case_hash": digest(CASES), "repeats": 2,
            "text_model": "qwen3.5-plus", "image_model": "qwen-image-2.0-pro",
            "control_generation": "local fixture; real control model only",
            "image_probe": {"count": 1, "size": "1024x1024", "prompt": IMAGE_PROMPT},
            "max_text_calls": 48, "max_text_calls_per_case": 8, "max_image_calls": 1,
            "max_estimated_cny": 6, "automatic_image_retry": False,
            "source_hashes": {name: hashlib.sha256((BACKEND / name).read_bytes()).hexdigest() for name in files}}


def install_budget_guard(cost_control, AppError, limit=6):
    """Bound cumulative reservations, including failed/uncertain/repaired calls."""
    original = cost_control.reserve
    state = {"estimated_micros": 0, "calls": [], "case": None, "case_text_calls": 0}

    def reserve(call_id, model, kind, units, **kwargs):
        prior = next((row for row in state["calls"] if row["call_id"] == call_id), None)
        if prior:
            return original(call_id, model, kind, units, **kwargs)
        count = sum(row["kind"] == kind for row in state["calls"])
        if kind not in {"text", "image"} or count >= (48 if kind == "text" else 1):
            raise AppError("EVAL_CALL_LIMIT", "验收请求次数达到上限", 429)
        if kind == "text" and state["case_text_calls"] >= 8:
            raise AppError("EVAL_CASE_LIMIT", "本案例请求次数达到上限", 429)
        estimate = cost_control._estimate(kind, units, cost_control._rate(model, kind))
        if state["estimated_micros"] + estimate > cost_control._money(limit):
            raise AppError("EVAL_BUDGET_LIMIT", "验收预算达到上限", 429)
        result = original(call_id, model, kind, units, **kwargs)
        state["estimated_micros"] += estimate
        state["case_text_calls"] += int(kind == "text")
        state["calls"].append({"call_id": call_id, "case": state["case"], "kind": kind,
                               "model": model, "estimated_micros": estimate})
        return result

    cost_control.reserve = reserve
    return state


async def live(output, plan):
    # Set before importing config/db; never point evaluation at the user's data.
    os.environ["DATA_DIR"] = str(output / "data")
    from app.core import config
    from app.core.errors import AppError
    from app.schemas.session import SessionCreate
    from app.services import cost_control, db, execution_store, session_store
    from app.services.agent_runtime import AgentRuntime
    from app.services.orchestrator import Orchestrator

    assert config.DATA_DIR.resolve() == (output / "data").resolve()
    config.settings.ensure_dirs()
    db.init_db()
    budget = install_budget_guard(cost_control, AppError)
    orch = Orchestrator()
    report = {"manifest_hash": digest(plan), "mode": "live", "cases": [], "image": None,
              "image_visual_review": "pending", "verdict": "incomplete"}

    def save():
        with db.connect() as conn:
            cost_control._ensure(conn)
            rows = conn.execute("SELECT call_id,model,kind,status,reserved_micros,charged_micros FROM model_cost_ledger").fetchall()
        report["ledger"] = [dict(row) for row in rows]
        report["budget"] = budget
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    async def execute(meta, stage, operation):
        eid, _ = execution_store.claim_session(meta.session_id, stage, "generate", {})
        started = time.monotonic()
        execution_store.launch(eid, operation)
        async for _ in execution_store.stream(eid):
            pass
        return {"execution_id": eid, "elapsed_seconds": round(time.monotonic() - started, 3),
                "execution": execution_store.snapshot("session", meta.session_id)}

    async def progress(*args):
        pass

    try:
        for case in CASES:
            for repeat in range(2):
                budget.update(case=f'{case["id"]}-{repeat + 1}', case_text_calls=0)
                meta = orch.create(SessionCreate(idea=case["idea"], orchestration_mode="multi_agent",
                    model_selection={"text": plan["text_model"], "image": plan["image_model"]}), owner_id="reliability-eval")
                generated = []

                async def generate():
                    generated.append(True)
                    meta.artifacts[case["stage"]] = {"evaluation_fixture": True, "idea": case["idea"],
                                                    "note": "仅验证控制流程，此处未生成实际内容"}

                async def operation():
                    runtime = AgentRuntime(orch, meta, case["stage"], progress, generate)
                    await runtime.run()
                    return {"type": "done"}

                result = await execute(meta, case["stage"], operation)
                run = session_store.load_session(meta.session_id).agent_runs.get(case["stage"], [{}])[-1]
                result.update(case=case["id"], repeat=repeat + 1, generated_count=len(generated), trace=run,
                              passed=result["execution"]["status"] == "completed" and 1 <= len(generated) <= (2 if case["stage"] == "script_generation" else 1))
                report["cases"].append(result)
                save()
                if any(row["status"] == "uncertain" for row in report["ledger"]):
                    report["stopped_reason"] = "unknown_request_outcome"
                    return
                if budget["estimated_micros"] >= cost_control._money(6):
                    report["stopped_reason"] = "budget_limit"
                    return
        budget.update(case="real-image", case_text_calls=0)
        meta = orch.create(SessionCreate(idea=IMAGE_PROMPT, model_selection={"image": plan["image_model"]}), owner_id="reliability-eval")
        image_path = output / "image-probe.png"

        async def image_operation():
            await asyncio.to_thread(orch.image.text_to_image, IMAGE_PROMPT, image_path,
                                    model=plan["image_model"], size="1024x1024")
            from PIL import Image
            with Image.open(image_path) as im:
                im.verify()
            return {"type": "done"}

        report["image"] = await execute(meta, "character_design", image_operation)
        report["image"]["path"] = str(image_path) if image_path.exists() else None
        report["verdict"] = "requires_visual_review" if all(c["passed"] for c in report["cases"]) and report["image"]["execution"]["status"] == "completed" else "failed"
    finally:
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly allow the frozen paid batch")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = (args.output or BACKEND.parent / "work" / "agent-reliability" / uuid.uuid4().hex).resolve()
    output.mkdir(parents=True, exist_ok=False)
    plan = manifest()
    (output / "manifest.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.live:
        asyncio.run(live(output, plan))
    print(json.dumps({"mode": "live" if args.live else "dry-run", "output": str(output),
                      "control_runs": 6, "image_requests": 1, "max_estimated_cny": 6}, ensure_ascii=False))


if __name__ == "__main__":
    main()
