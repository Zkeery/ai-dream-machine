"""Reset only the isolated recording account; retain a recoverable DB backup."""
import asyncio
from datetime import datetime
import json
import sqlite3
import time
from demo_fixtures import ASSETS, DATA, INVITE, ROOT, configure, make_orchestrator

configure()
from app.main import app  # initializes only the guarded demo database
from app.core import config
from app.schemas.session import SessionCreate
from app.services import auth, db, execution_store, recovery, session_store


async def run_stage(orch, session_id, stage):
    execution_id, _ = execution_store.claim_session(session_id, stage, "generate", {})
    recovery.capture(execution_id, recovery.session_input(orch.get(session_id), stage, {}))
    async def run():
        result = await orch.execute_stage(session_id, stage, execution_store.progress(execution_id))
        return {"type": "done", "session": result.model_dump()}
    execution_store.launch(execution_id, run)
    async for _ in execution_store.stream(execution_id):
        pass
    return orch.get(session_id)


async def reset():
    if config.DATA_DIR != DATA.resolve() or not config.settings.mode_mock or not config.settings.disable_outbound_network:
        raise RuntimeError("重置只允许操作 .runtime/release-demo/data")
    for name in [*(f"walk-{i:02}.png" for i in (1, 2, 3)), *(f"shot-{i:02}.mp4" for i in (1, 2, 3))]:
        if not (ASSETS / name).is_file():
            raise RuntimeError(f"缺少本地素材：{name}")
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM executions WHERE status IN ('pending','running')").fetchone():
            raise RuntimeError("有作品正在生成，请完成后再重置。")
        conn.execute("INSERT OR IGNORE INTO invite_codes (code,status,created_at) VALUES (?,?,?)", (INVITE, "unused", time.time()))
    owner = auth._bind_user(INVITE)["user_id"]
    backup = ROOT / ".runtime/release-demo/backups" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with db.connect() as source, sqlite3.connect(backup) as target:
        source.backup(target)
    old = session_store.list_sessions(owner)
    created = []
    try:
        for interrupted in (False, True):
            orch = make_orchestrator(interrupt_video=interrupted)
            idea = "雨夜归途：雨后的灯街上，黑色卷毛小狗墨豆和主人慢慢走回家。" + ("继续完成后半段。" if interrupted else "让急促的脚步慢下来。")
            meta = orch.create(SessionCreate(idea=idea, orchestration_mode="workflow", expand_idea=False), owner_id=owner)
            created.append(meta.session_id)
            for stage in orch.stage_order(meta):
                meta = await run_stage(orch, meta.session_id, stage)
                if meta.status == "failed":
                    if not interrupted or stage != "video_generation":
                        raise RuntimeError(meta.error)
                    break
                if stage != "post_production":
                    orch.continue_session(meta.session_id)
            if interrupted:
                if not recovery.status(meta, "session")["can_resume"]:
                    raise RuntimeError("继续制作样例未形成可恢复检查点")
            else:
                # Complete fixture, with the script tab as its opening view.
                # Keep the existing completed-work regeneration lock intact.
                meta = orch.continue_session(meta.session_id)
                meta.current_stage = "script_generation"
                session_store.touch(meta)
            report = next(r for r in reversed(meta.creation_checks) if r["stage"] == "script_generation")
            if not 2 <= len(report["findings"]) <= 3:
                raise RuntimeError("示例必须由真实检查器识别出 2–3 处问题")
        for meta in old:
            session_store.delete_session(meta.session_id, owner)
        result = {"complete": created[0], "resume": created[1], "backup": str(backup),
                  "url": f"http://127.0.0.1:3030/?session={created[0]}"}
        (ROOT / ".runtime/release-demo/recording-pair.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        for session_id in created:
            session_store.delete_session(session_id, owner)
        raise


if __name__ == "__main__":
    asyncio.run(reset())
