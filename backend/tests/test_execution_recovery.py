# -*- coding: utf-8 -*-
"""Execution isolation, reconnect, deduplication and persistent process recovery.

All records live in pytest temporary directories. No model/service/network calls.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.api import sessions as sessions_api, tasks as tasks_api
from app.core import config
from app.core.errors import AppError
from app.schemas.session import SessionMeta
from app.schemas.task import TaskCreate, TaskMeta
from app.services import db, execution_store as executions, session_store, task_store


def _session(session_id="story1", **kwargs):
    return session_store.create_session(SessionMeta(session_id=session_id, owner_id="owner",
                                                    idea="保留的创意", current_stage="script_generation", **kwargs))


def _events(chunks):
    return [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines() if line.startswith("data: ")]


async def _collect(stream):
    return _events([chunk async for chunk in stream])


class SlowOrchestrator:
    def __init__(self, release):
        self.release = release
        self.calls = 0

    def get(self, session_id):
        return session_store.load_session(session_id)

    async def execute_stage(self, session_id, stage, progress):
        self.calls += 1
        await progress(stage, "实际进度", 25)
        await self.release.wait()
        meta = self.get(session_id)
        meta.artifacts[stage] = {"title": "完成后可查询"}
        meta.status = "stage_completed"
        meta.current_stage = stage
        meta.stages_completed.append(stage)
        session_store.touch(meta)
        await progress(stage, "生成完成", 100)
        return meta


def test_story_disconnect_reconnect_and_two_observers(data_dirs):
    _session()

    async def run():
        release = asyncio.Event()
        orch = SlowOrchestrator(release)
        response = await sessions_api.execute_stage("story1", "script_generation", "owner", orch, "story-submit")
        initial = await anext(response.body_iterator)
        assert _events([initial])[0]["percent"] == 0
        await response.body_iterator.aclose()  # Browser navigation closes only the subscriber.
        await asyncio.sleep(0)
        snapshot = await sessions_api.get_session("story1", "owner", orch)
        assert snapshot["status"] == "running"
        assert snapshot["execution"]["last_event"]["percent"] == 25

        repeated = await sessions_api.execute_stage("story1", "script_generation", "owner", orch, "story-submit")
        aliased = await sessions_api.execute_stage("story1", "script_generation", "owner", orch, "parallel-submit")
        await aliased.body_iterator.aclose()
        reconnect = await sessions_api.stream_session("story1", None, "owner", orch)
        a = asyncio.create_task(_collect(repeated.body_iterator))
        b = asyncio.create_task(_collect(reconnect.body_iterator))
        release.set()
        left, right = await asyncio.wait_for(asyncio.gather(a, b), 3)
        assert left == right
        assert left[-1]["type"] == "done"
        assert any(event.get("percent") == 25 for event in left)
        assert orch.calls == 1
        saved = await sessions_api.get_session("story1", "owner", orch)
        assert saved["status"] == "stage_completed"
        assert saved["execution"]["status"] == "completed"
        assert (await _collect(executions.stream(saved["execution"]["execution_id"]))) == left
        completed_retry = await sessions_api.execute_stage("story1", "script_generation", "owner", orch, "story-submit")
        assert await _collect(completed_retry.body_iterator) == left
        aliased_retry = await sessions_api.execute_stage("story1", "script_generation", "owner", orch, "parallel-submit")
        assert await _collect(aliased_retry.body_iterator) == left
        assert orch.calls == 1
        assert executions._workers == {}

    asyncio.run(run())


def test_atomic_story_claim_deduplicates_and_blocks_other_stage(data_dirs):
    _session()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: executions.claim_session("story1", "script_generation", "generate", {}), range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    assert session_store.load_session("story1").status == "running"
    with pytest.raises(AppError, match="当前项目正在执行"):
        executions.claim_session("story1", "storyboard", "generate", {})


def test_short_task_idempotency_disconnect_and_multi_observer(data_dirs, monkeypatch):
    async def run():
        release = asyncio.Event()

        class Pipeline:
            calls = 0

            async def run(self, task_id, task_type, inputs, progress):
                self.calls += 1
                await progress("video", "等待生成", 45)
                await release.wait()
                return {"final_video": "/temporary/final.mp4", "source": inputs["text"]}

        pipeline = Pipeline()
        monkeypatch.setattr(tasks_api, "get_pipelines", lambda: pipeline)
        req = TaskCreate(type="literary_video", input={"text": "保留文案"})
        first = await tasks_api.create_task(req, "owner", "submit-one")
        second = await tasks_api.create_task(req, "owner", "submit-one")
        legacy_repeat = await tasks_api.create_task(req, "owner", None)
        aliased_repeat = await tasks_api.create_task(req, "owner", "another-active-submit")
        assert first == second == legacy_repeat == aliased_repeat
        task_id = first["task_id"]
        response = await tasks_api.stream_task(task_id, "owner", None)
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        await asyncio.sleep(0)
        assert (await tasks_api.get_task(task_id, "owner"))["execution"]["last_event"]["percent"] == 45
        left = asyncio.create_task(_collect((await tasks_api.stream_task(task_id, "owner", None)).body_iterator))
        right = asyncio.create_task(_collect((await tasks_api.stream_task(task_id, "owner", None)).body_iterator))
        release.set()
        a, b = await asyncio.wait_for(asyncio.gather(left, right), 3)
        assert a == b and a[-1]["type"] == "done"
        assert pipeline.calls == 1
        assert task_store.load_task(task_id).status == "completed"
        # Retrying the same network request after completion reuses the saved result.
        assert await tasks_api.create_task(req, "owner", "submit-one") == first
        assert await tasks_api.create_task(req, "owner", "another-active-submit") == first
        assert len(task_store.list_tasks("owner")) == 1
        assert executions._workers == {}
        with pytest.raises(AppError, match="输入不同"):
            await tasks_api.create_task(TaskCreate(type="literary_video", input={"text": "另一个输入"}), "owner", "submit-one")
        # An explicit fresh attempt has its own task/result and retains the old one.
        retry = await tasks_api.create_task(req, "owner", "explicit-new-attempt")
        assert retry["task_id"] != task_id
        assert (await _collect((await tasks_api.stream_task(retry["task_id"], "owner", None)).body_iterator))[-1]["type"] == "done"
        assert pipeline.calls == 2
        assert len(task_store.list_tasks("owner")) == 2

    asyncio.run(run())


def test_execution_failure_keeps_input_result_and_readable_error(data_dirs, monkeypatch):
    async def run():
        class Pipeline:
            async def run(self, *args):
                raise AppError("MODEL_TIMEOUT", "生成服务超时，请稍后重试")

        monkeypatch.setattr(tasks_api, "get_pipelines", Pipeline)
        result = await tasks_api.create_task(TaskCreate(type="literary_video", input={"text": "输入不能丢"}), "owner", "failure")
        events = await _collect((await tasks_api.stream_task(result["task_id"], "owner", None)).body_iterator)
        assert events[-1]["error"]["code"] == "MODEL_TIMEOUT"
        meta = await tasks_api.get_task(result["task_id"], "owner")
        assert meta["input"]["text"] == "输入不能丢"
        assert meta["status"] == "failed" and meta["error"] == "生成服务超时，请稍后重试"
        assert meta["execution"]["status"] == "failed"
    asyncio.run(run())


def test_shutdown_interrupts_without_erasing_prior_artifacts(data_dirs):
    _session(artifacts={"script_generation": {"title": "旧产物"}})

    async def run():
        release = asyncio.Event()
        orch = SlowOrchestrator(release)
        response = await sessions_api.execute_stage("story1", "script_generation", "owner", orch, None)
        await response.body_iterator.aclose()
        await asyncio.sleep(0)
        await executions.shutdown()
        meta = session_store.load_session("story1")
        assert meta.status == "failed"
        assert meta.artifacts["script_generation"]["title"] == "旧产物"
        snapshot = executions.snapshot("session", "story1")
        assert snapshot["status"] == "interrupted"
        events = await _collect(executions.stream(snapshot["execution_id"]))
        assert events[-1]["error"]["code"] == "EXECUTION_INTERRUPTED"
        assert executions._workers == {}
    asyncio.run(run())


def test_shutdown_before_worker_start_persists_interruption(data_dirs):
    _session()

    async def run():
        execution_id, _ = executions.claim_session("story1", "script_generation", "generate", {})
        async def unexpected():
            pytest.fail("A canceled execution must never call a model")
        executions.launch(execution_id, unexpected)
        await executions.shutdown()
        assert executions.snapshot("session", "story1")["status"] == "interrupted"
        assert not executions._workers
    asyncio.run(run())


def test_restart_finalizes_already_persisted_completion(data_dirs):
    _session()
    execution_id, _ = executions.claim_session("story1", "script_generation", "generate", {})
    meta = session_store.load_session("story1")
    meta.status = "stage_completed"
    meta.artifacts = {"script_generation": {"title": "已经完成"}}
    session_store.touch(meta)
    # Simulate a crash between persisting the result and its done event.
    assert executions.recover_unfinished()["session"] == 0
    assert executions.snapshot("session", "story1")["status"] == "completed"
    assert asyncio.run(_collect(executions.stream(execution_id)))[-1]["session"]["artifacts"] == meta.artifacts


def test_restart_finalizes_persisted_short_task_result(data_dirs):
    meta = TaskMeta(task_id="saved-result", type="literary_video", owner_id="owner", input={"text": "保留"})
    _, execution_id, _ = executions.create_task(meta, "saved-result-request")
    task_store.mark_completed(meta, {"final_video": "/temporary/saved.mp4"})
    executions.recover_unfinished()
    assert executions.snapshot("task", meta.task_id)["status"] == "completed"
    assert asyncio.run(_collect(executions.stream(execution_id)))[-1]["task"]["result"]["final_video"].endswith("saved.mp4")


def test_legacy_pending_and_running_are_interrupted_once(data_dirs):
    _session(status="running", artifacts={"script_generation": {"title": "保留"}})
    task_store.create_task(TaskMeta(task_id="legacy-task", type="literary_video", owner_id="owner", input={"text": "保留"}))
    assert executions.recover_unfinished() == {"session": 1, "task": 1}
    assert executions.recover_unfinished() == {"session": 0, "task": 0}
    assert task_store.load_task("legacy-task").status == "failed"
    assert executions.snapshot("task", "legacy-task")["status"] == "interrupted"
    assert session_store.load_session("story1").artifacts["script_generation"]["title"] == "保留"


def test_new_metadata_roundtrip_and_idempotent_old_schema_upgrade(tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    old_schema = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS executions")[0]
    with db.connect() as conn:
        conn.executescript(old_schema)
        conn.execute("INSERT INTO sessions(session_id,owner_id,idea,artifacts,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     ("old", "owner", "老项目", '{"script_generation":{"title":"原产物"}}', 1, 1))
    db.init_db()
    db.init_db()
    old = session_store.load_session("old")
    assert old.idea == "老项目" and old.owner_id == "owner"
    assert old.artifacts["script_generation"]["title"] == "原产物"
    assert old.stale_stages == []
    old.stale_stages = ["post_production"]
    old.artifact_versions = {"script_generation": [{"version_id": "v1", "artifact": {"title": "原产物"}}]}
    old.selected_versions = {"script_generation": "v1"}
    old.execution_inputs = [{"execution_id": "input1", "input_versions": {"script_generation": "v1"}}]
    session_store.touch(old)
    loaded = session_store.list_sessions("owner")[0]
    assert loaded.stale_stages == old.stale_stages
    assert loaded.artifact_versions == old.artifact_versions
    assert loaded.selected_versions == old.selected_versions
    assert loaded.execution_inputs == old.execution_inputs


def test_fresh_application_process_recovers_crashed_work(tmp_path):
    """Two actual app processes/lifespans share only an isolated temporary DB."""
    backend = Path(__file__).resolve().parents[1]
    prefix = """
import asyncio,json,os
import dotenv
dotenv.load_dotenv=lambda *a,**kw: False
from app.main import app, lifespan
from app.schemas.session import SessionMeta
from app.schemas.task import TaskMeta
from app.services import session_store,execution_store as ex,task_store
"""
    first = prefix + """
async def run():
    async with lifespan(app):
        m=SessionMeta(session_id='crash-story',owner_id='owner',idea='原输入',current_stage='script_generation',
          artifacts={'script_generation':{'title':'历史产物'}},stale_stages=['post_production'],
          selected_versions={'script_generation':'v1'},
          artifact_versions={'script_generation':[{'version_id':'v1','artifact':{'title':'历史产物'}}]},
          execution_inputs=[{'input_versions':{'script_generation':'v1'}}])
        session_store.create_session(m)
        eid,_=ex.claim_session('crash-story','script_generation','generate',{})
        await ex.progress(eid)('script_generation','进度已保存',35)
        ex.create_task(TaskMeta(task_id='crash-task',type='literary_video',owner_id='owner',input={'text':'原文案'}),'original-submit')
        os._exit(0)
asyncio.run(run())
"""
    second = prefix + """
async def run():
    async with lifespan(app):
        m=session_store.load_session('crash-story'); t=task_store.load_task('crash-task')
        assert m.status=='failed' and t.status=='failed'
        assert m.idea=='原输入' and m.artifacts['script_generation']['title']=='历史产物'
        assert m.stale_stages==['post_production'] and m.selected_versions['script_generation']=='v1'
        assert m.artifact_versions['script_generation'][0]['version_id']=='v1' and m.execution_inputs
        assert t.input['text']=='原文案'
        s=ex.snapshot('session','crash-story')
        assert s['status']=='interrupted' and s['last_event']['percent']==35
        assert ex.snapshot('task','crash-task')['status']=='interrupted'
        assert not ex._workers
        with __import__('app.services.db',fromlist=['connect']).connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM executions').fetchone()[0]==2
        print(json.dumps({'statuses':[m.status,t.status],'progress':s['last_event']['percent'],'model_calls':0}))
asyncio.run(run())
"""
    env = {**os.environ, "DATA_DIR": str(tmp_path / "restart-data"), "PYTHONPATH": str(backend)}
    assert subprocess.run([sys.executable, "-c", first], cwd=backend, env=env, capture_output=True, text=True, timeout=15).returncode == 0
    result = subprocess.run([sys.executable, "-c", second], cwd=backend, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip())["progress"] == 35


def test_session_and_task_streams_enforce_ownership(data_dirs):
    _session()
    task_store.create_task(TaskMeta(task_id="private-task", type="literary_video", owner_id="owner"))
    async def run():
        with pytest.raises(AppError) as error:
            await sessions_api.stream_session("story1", None, "other", SlowOrchestrator(asyncio.Event()))
        assert error.value.status_code == 404
        with pytest.raises(AppError) as error:
            await tasks_api.stream_task("private-task", "other", None)
        assert error.value.status_code == 404
    asyncio.run(run())


def test_export_stale_block_and_explicit_historical_version(data_dirs):
    final = config.VIDEO_DIR / "story1" / "old.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"historical test video")
    meta = _session(artifacts={"post_production": {"final_video": str(final)}},
                    stale_stages=["post_production"],
                    artifact_versions={"post_production": [{"version_id": "old-version", "reason": "legacy", "artifact": {"final_video": str(final)}}]})
    async def run():
        orch = SlowOrchestrator(asyncio.Event())
        with pytest.raises(AppError) as error:
            await sessions_api.export_session(meta.session_id, None, "owner", orch)
        assert error.value.code == "STALE_EXPORT"
        response = await sessions_api.export_session(meta.session_id, "old-version", "owner", orch)
        assert Path(response.path) == final.resolve()
        with pytest.raises(AppError) as error:
            await sessions_api.export_session(meta.session_id, "nonexistent", "owner", orch)
        assert error.value.code == "VERSION_NOT_FOUND"
    asyncio.run(run())


def test_http_story_execution_contract_and_completed_request_replay(orch, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api.deps import get_current_user, get_orchestrator
    from app.main import app

    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: "owner")
    monkeypatch.setitem(app.dependency_overrides, get_orchestrator, lambda: orch)
    calls = []
    original = orch.llm.generate_json
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(orch.llm, "generate_json", counted)
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={"idea": "HTTP 合同测试"}).json()["session_id"]
        url = f"/api/sessions/{session_id}/execute/script_generation"
        result = client.post(url, headers={"Idempotency-Key": "http-submit"})
        assert result.status_code == 200
        events = _events([result.text])
        assert events[0]["type"] == "progress" and events[-1]["type"] == "done"
        assert events[-1]["session"]["status"] == "stage_completed"
        assert _events([client.post(url, headers={"Idempotency-Key": "http-submit"}).text]) == events
        assert _events([client.get(f"/api/sessions/{session_id}/stream").text]) == events
        assert client.get(f"/api/sessions/{session_id}").json()["execution"]["status"] == "completed"
        assert len(calls) == 1


def test_parent_execution_context_isolated_between_concurrent_workers(data_dirs):
    _session("context-one")
    _session("context-two")
    async def run():
        release = asyncio.Event()
        observed = {}
        jobs = []
        for session_id in ("context-one", "context-two"):
            execution_id, _ = executions.claim_session(session_id, "script_generation", "generate", {})
            async def operation(session_id=session_id, execution_id=execution_id):
                assert executions.current_execution_id() == execution_id
                await release.wait()
                observed[session_id] = await asyncio.to_thread(executions.current_execution_id)
                return {"type": "done", "session": {"session_id": session_id}}
            executions.launch(execution_id, operation)
            jobs.append((session_id, execution_id))
        await asyncio.sleep(0)
        assert executions.current_execution_id() is None
        release.set()
        await asyncio.gather(*(_collect(executions.stream(execution_id)) for _, execution_id in jobs))
        assert observed == dict(jobs)
        assert executions.current_execution_id() is None
    asyncio.run(run())


def _saved_partial(session_id, parent_id, ledger_status="completed", finished_at=None):
    meta = session_store.load_session(session_id)
    meta.status = "idle"
    meta.current_stage = "video_generation"
    meta.stale_stages = ["video_generation", "post_production"]
    meta.artifacts = {"video_generation": {"stale_items": ["s2"],
                     "segments": [{"shot_id": "s1", "path": "/temporary/new-s1.mp4"},
                                  {"shot_id": "s2", "path": "/temporary/old-s2.mp4"}],
                     "item_input_versions": {"s1": {"reference_generation": "r-new"},
                                             "s2": {"reference_generation": "r-old"}}},
                      "post_production": {"final_video": "/temporary/old-final.mp4"}}
    meta.execution_inputs = [{"execution_id": "inner-ledger", "parent_execution_id": parent_id,
                              "stage": "video_generation", "status": ledger_status,
                              "target_ids": ["s1"], "prompts": {"s1": "保存的镜头提示词"},
                              "descriptions": {"s1": "保存的镜头描述"},
                              "finished_at": time.time() if finished_at is None else finished_at}]
    session_store.touch(meta)
    return meta


def test_restart_recovers_finished_target_run_with_remaining_stale(data_dirs):
    _session("targeted-result")
    execution_id, _ = executions.claim_session("targeted-result", "video_generation", "regenerate", {"target_ids": ["s1"]})
    original = _saved_partial("targeted-result", execution_id)
    assert executions.recover_unfinished()["session"] == 0
    saved = session_store.load_session("targeted-result")
    assert executions.snapshot("session", saved.session_id)["status"] == "completed"
    assert saved.status == "idle"
    assert saved.artifacts == original.artifacts
    assert saved.stale_stages == original.stale_stages
    assert saved.execution_inputs == original.execution_inputs
    events = asyncio.run(_collect(executions.stream(execution_id)))
    assert events[-1]["type"] == "done" and events[-1]["session"]["status"] == "idle"
    from app.services.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    with pytest.raises(AppError) as error:
        orch.continue_session(saved.session_id)
    assert error.value.code in ("NOT_READY", "STALE_STAGE")
    async def export():
        with pytest.raises(AppError) as error:
            await sessions_api.export_session(saved.session_id, None, "owner", orch)
        assert error.value.code == "STALE_EXPORT"
    asyncio.run(export())


@pytest.mark.parametrize("kind", ["wrong_parent", "unfinished", "failed", "old_timestamp", "still_running"])
def test_restart_does_not_confuse_old_or_unfinished_target_ledger(data_dirs, kind):
    _session("unconfirmed-target")
    execution_id, _ = executions.claim_session("unconfirmed-target", "video_generation", "regenerate", {"target_ids": ["s1"]})
    meta = _saved_partial("unconfirmed-target", "previous-parent" if kind == "wrong_parent" else execution_id,
                          ledger_status="running" if kind == "unfinished" else "failed" if kind == "failed" else "completed",
                          finished_at=1 if kind == "old_timestamp" else None)
    if kind == "still_running":
        meta.status = "running"
        session_store.touch(meta)
    assert executions.recover_unfinished()["session"] == 1
    saved = session_store.load_session(meta.session_id)
    assert executions.snapshot("session", meta.session_id)["status"] == "interrupted"
    assert saved.status == "failed"
    assert saved.artifacts == meta.artifacts and saved.execution_inputs == meta.execution_inputs


def test_fresh_process_recovers_saved_partial_target_result(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    prefix = """
import asyncio,json,os,time
import dotenv
dotenv.load_dotenv=lambda *a,**kw: False
from app.main import app,lifespan
from app.schemas.session import SessionMeta
from app.services import session_store,execution_store as ex
"""
    first = prefix + """
async def run():
    async with lifespan(app):
        session_store.create_session(SessionMeta(session_id='partial',owner_id='owner',idea='原输入',current_stage='video_generation'))
        eid,_=ex.claim_session('partial','video_generation','regenerate',{'target_ids':['s1']})
        m=session_store.load_session('partial');m.status='idle'
        m.stale_stages=['video_generation','post_production']
        m.artifacts={'video_generation':{'stale_items':['s2'],'prompts':{'s1':'原镜头输入'},'item_input_versions':{'s1':{'reference_generation':'new'},'s2':{'reference_generation':'old'}}}}
        m.execution_inputs=[{'parent_execution_id':eid,'stage':'video_generation','status':'completed','finished_at':time.time(),'target_ids':['s1']}]
        session_store.touch(m)
        os._exit(0)
asyncio.run(run())
"""
    second = prefix + """
async def run():
    async with lifespan(app):
        m=session_store.load_session('partial');s=ex.snapshot('session','partial')
        assert m.status=='idle' and s['status']=='completed'
        assert m.stale_stages==['video_generation','post_production']
        assert m.artifacts['video_generation']['stale_items']==['s2']
        assert m.artifacts['video_generation']['prompts']['s1']=='原镜头输入'
        assert m.artifacts['video_generation']['item_input_versions']['s2']['reference_generation']=='old'
        events=[json.loads(line[6:]) async for chunk in ex.stream(s['execution_id']) for line in chunk.splitlines() if line.startswith('data: ')]
        assert events[-1]['type']=='done' and events[-1]['session']['status']=='idle'
        print(json.dumps({'execution':'completed','stage':'idle','remaining':['s2'],'model_calls':0}))
asyncio.run(run())
"""
    env = {**os.environ, "DATA_DIR": str(tmp_path / "partial-restart"), "PYTHONPATH": str(backend)}
    result = subprocess.run([sys.executable, "-c", first], cwd=backend, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([sys.executable, "-c", second], cwd=backend, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip())["remaining"] == ["s2"]
