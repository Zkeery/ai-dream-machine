"""Bounded, explicitly requested real-provider smoke. One stage per command.

Uses isolated project data; never prints secrets and never disables review.
Run from backend: .venv/bin/python scripts/real_acceptance.py create story
Then step <story|comic> <stage>, inspect, and confirm <story|comic>.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
DATA = PROJECT / ".runtime" / "real-acceptance-20261004"
EVIDENCE = PROJECT / "docs" / "evidence" / "真实样片验收"
os.environ["DATA_DIR"] = str(DATA)
os.environ["MONTHLY_BUDGET_CNY"] = "50"
os.environ["MONTHLY_USER_BUDGET_CNY"] = "50"
sys.path.insert(0, str(PROJECT / "backend"))

from app.core import config
from app.schemas.session import SessionCreate
from app.services import db, execution_store, session_store, cost_control, recovery, provider_jobs
from app.services.orchestrator import Orchestrator

OWNER = "real-acceptance-20261004"
MANIFEST = EVIDENCE / "run.json"
IDEA = "雨后的海边灯塔，一位穿深蓝外套、系红围巾的青年林舟，在灯塔门前拾起一封信，然后望向亮起的灯塔。严格限定：只有林舟一名角色，只有海边灯塔一处场景，只有一集，故事只包含拾信和望塔两个镜头，不增加其他人物、回忆或地点。台词只用两句：雨停了。灯塔在等我。"


def read_manifest():
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {
        "run_id": "real-20261004-v1", "created_at": time.time(), "cases": {}, "events": [],
        "protocol": "目标与评分.md", "paid_provider_calls": True,
        "provider_reported_cny": None, "quality_verdict": "NEED_REVIEW",
    }


def save_manifest(data):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def source_versions():
    roots = [PROJECT / "backend" / "app", PROJECT / "backend" / "prompts"]
    paths = [p for root in roots if root.exists() for p in root.rglob("*")
             if p.is_file() and p.suffix in {".py", ".md", ".j2", ".txt"}]
    paths.append(PROJECT / "backend" / "model-cost-rates.json")
    return {str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths) if p.exists()}


def validate_scope(meta, stage):
    script = meta.artifacts.get("script_generation") or {}
    if stage != "script_generation":
        counts = {k: len(script.get(k, [])) for k in ("characters", "settings", "episodes")}
        if counts != {"characters": 1, "settings": 1, "episodes": 1}:
            raise ValueError("先人工核对并编辑剧本范围：" + str(counts))
    board = meta.artifacts.get("comic_storyboard" if meta.project_type == "comic" else "storyboard") or {}
    if stage in {"reference_generation", "video_generation", "comic_panels", "comic_audio", "comic_composition", "post_production"}:
        shots = board.get("shots", [])
        if not 1 <= len(shots) <= 2:
            raise ValueError("分镜超出1–2镜头，停止付费生成")
        if meta.project_type == "comic" and any(len(s.get("dialogues", [])) > 2 for s in shots):
            raise ValueError("每镜头对白超过2句，先人工编辑")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["create", "step", "resume", "recover-process", "regenerate", "edit", "confirm", "inspect"])
    ap.add_argument("kind", choices=["story", "comic"])
    ap.add_argument("stage", nargs="?")
    ap.add_argument("--request")
    ap.add_argument("--reconcile-402", action="store_true")
    args = ap.parse_args()
    config.settings.ensure_dirs()
    db.init_db()
    if not config.settings.content_review_enabled:
        raise RuntimeError("真实验收要求沿用内容审核，不允许关闭")
    manifest = read_manifest()
    orch = Orchestrator()
    case = manifest["cases"].get(args.kind)
    if args.action == "create":
        if case:
            raise RuntimeError("验收案例已存在，请继续原案例，避免重复费用")
        req = SessionCreate(project_type=args.kind, idea=IDEA,
            style="cinematic realistic" if args.kind == "story" else "cinematic manga",
            video_ratio="16:9" if args.kind == "story" else "9:16", episodes=1,
            video_generation_mode="first_frame", resolution="720P",
            model_selection={"text": "qwen3.5-plus", "image": "qwen-image-2.0",
                             **({"video_first_frame": "wan2.7-i2v"} if args.kind == "story" else {})})
        meta = orch.create(req, owner_id=OWNER)
        manifest["cases"][args.kind] = {"session_id": meta.session_id, "stages": [], "created_at": time.time()}
    else:
        if not case:
            raise RuntimeError("先创建案例")
        meta = orch.get(case["session_id"])
        if args.action == "recover-process":
            recovered = execution_store.recover_unfinished()
            meta = orch.get(meta.session_id)
            manifest["events"].append({"case": args.kind, "operation": "controlled_process_restart", "result": recovered,
                "stage": meta.current_stage, "status": meta.status, "at": time.time()})
        elif args.action == "confirm":
            meta = orch.continue_session(meta.session_id)
        elif args.action == "resume":
            source_id = execution_store.snapshot("session", meta.session_id)["execution_id"]
            if args.reconcile_402:
                with db.connect() as conn:
                    rejected = conn.execute("SELECT id FROM provider_jobs WHERE scope_execution_id=? AND status='failed'", (source_id,)).fetchall()
                for row in rejected:
                    provider_jobs.reconcile_legacy_http_402(row[0], OWNER)
            source, request = recovery.prepare(meta, "session", source_id)
            eid, created = execution_store.claim_resume(source_id, OWNER, request_key=f"acceptance:{source_id}")
            recovery.copy_snapshot(source_id, eid)
            started = time.time()
            async def resume_work():
                current = orch.get(meta.session_id)
                recovery.verify(source_id, recovery.session_input(current, source["stage"], request))
                with provider_jobs.resume_scope(source_id, OWNER, allow_new=True):
                    result = (await orch.intervene(meta.session_id, source["stage"], request, execution_store.progress(eid))
                              if source["operation"] == "regenerate" else
                              await orch.execute_stage(meta.session_id, source["stage"], execution_store.progress(eid)))
                return {"type": "done", "session": result.model_dump()}
            if created:
                execution_store.launch(eid, resume_work)
            async for _ in execution_store.stream(eid):
                pass
            meta = orch.get(meta.session_id)
            record = {"stage": source["stage"], "operation": "resume", "execution_id": eid,
                      "source_execution_id": source_id, "status": meta.status,
                      "elapsed_seconds": round(time.time()-started, 3), "error": meta.error}
            case["stages"].append(record)
            manifest["events"].append(record)
        elif args.action == "edit":
            edits = json.loads(Path(args.stage).read_text())
            stage = meta.current_stage
            request = {"operation": "save", "artifact": edits}
            eid, created = execution_store.claim_session(meta.session_id, stage, "save", request,
                f"{args.kind}:{stage}:edit:{execution_store.fingerprint(edits)}")
            async def save_edit():
                result = await orch.intervene(meta.session_id, stage, request, execution_store.progress(eid))
                return {"type": "done", "session": result.model_dump()}
            if created:
                execution_store.launch(eid, save_edit)
            async for _ in execution_store.stream(eid):
                pass
            meta = orch.get(meta.session_id)
            manifest["events"].append({"case": args.kind, "stage": stage, "operation": "manual_scope_edit",
                "execution_id": eid, "input": str(Path(args.stage).resolve().relative_to(PROJECT)),
                "status": meta.status, "error": meta.error})
        elif args.action in {"step", "regenerate"}:
            stage = args.stage or meta.current_stage
            if args.action == "step" and (stage != meta.current_stage or meta.status in {"stage_completed", "session_completed"}):
                raise RuntimeError("先检查当前产物并确认；禁止跳阶段或重复成功生成")
            validate_scope(meta, stage)
            operation = "regenerate" if args.action == "regenerate" else "generate"
            request = {"operation": "regenerate"} if operation == "regenerate" else {}
            if args.request:
                request.update(json.loads(Path(args.request).read_text()))
            eid, created = execution_store.claim_session(meta.session_id, stage, operation, request, f"{args.kind}:{stage}:{operation}:{execution_store.fingerprint(request)}")
            if not created:
                raise RuntimeError("本阶段已提交，先查看记录，禁止盲目重试")
            recovery.capture(eid, recovery.session_input(meta, stage, request))
            manifest.setdefault("initial_source_versions", source_versions())
            save_manifest(manifest)
            started = time.time()
            async def run():
                result = (await orch.intervene(meta.session_id, stage, request, execution_store.progress(eid))
                          if operation == "regenerate" else
                          await orch.execute_stage(meta.session_id, stage, execution_store.progress(eid)))
                return {"type": "done", "session": result.model_dump()}
            execution_store.launch(eid, run)
            async for _ in execution_store.stream(eid):
                pass
            meta = orch.get(meta.session_id)
            record = {"stage": stage, "operation": operation, "execution_id": eid, "status": meta.status,
                      "elapsed_seconds": round(time.time()-started, 3), "error": meta.error}
            case["stages"].append(record)
            manifest["events"].append(record)
    manifest["usage"] = cost_control.usage(OWNER)
    manifest.setdefault("initial_source_versions", source_versions())
    manifest["latest_source_versions"] = source_versions()
    manifest["updated_at"] = time.time()
    save_manifest(manifest)
    print(json.dumps({"case": args.kind, "session_id": meta.session_id, "stage": meta.current_stage,
                      "status": meta.status, "error": meta.error, "usage": manifest["usage"]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
