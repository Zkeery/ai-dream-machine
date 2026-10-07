#!/usr/bin/env python3
"""Real local retrieval + real HTTP/SSE contracts, with an explicit StubLLM.

Everything except the shared read-only model cache runs in a temporary project
directory. No production tokens, databases, documents, or paid models are used.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.core import config  # noqa: E402

# Capture before redirecting every business data path.
MODEL_CACHE = Path(os.getenv("KNOWLEDGE_MODEL_CACHE", str(config.DATA_DIR / "knowledge" / "models")))
os.environ["KNOWLEDGE_MODEL_CACHE"] = str(MODEL_CACHE)


class StubLLM:
    """Fixed structured responses; records context delivery, not model quality."""
    def __init__(self):
        self.calls = []

    def generate(self, *args, **kwargs):
        raise AssertionError("Idea expansion was not requested")

    def generate_json(self, system, user, model_cls):
        self.calls.append({"model_class": model_cls.__name__, "has_constraint": "手表确认时间" in user,
                           "has_role_fact": "钟表修理师" in user, "has_untrusted_data_boundary": "不是对你的指令" in system})
        if model_cls.__name__ == "StoryboardArtifact":
            payload = {"shots": [
                {"shot_id": "s1", "episode_number": 1, "description": "林澈在港口钟楼修理手表，先确认时间。", "prompt": "cinematic harbour watchmaker", "character_ids": ["c1"], "setting_ids": ["l1"]},
                {"shot_id": "s2", "episode_number": 1, "description": "她停在岸边，把救生绳交给水手。", "prompt": "watchmaker on shore hands rope to sailor", "character_ids": ["c1"], "setting_ids": ["l1"]},
            ]}
        else:
            payload = {
                "title": "夜航 · 验收样例（生成器替身）", "logline": "不会游泳的钟表修理师林澈在港口协助一次救援。",
                "genre": ["剧情"], "mood": "克制",
                "characters": [{"name": "林澈", "character_id": "c1", "description": "钟表修理师，不会游泳，行动前查看手表。", "role": "主角"}],
                "settings": [{"name": "港口钟楼", "setting_id": "l1", "description": "夜色下的旧钟楼与码头。"}],
                "episodes": [{"episode_number": 1, "act_title": "岸边的选择", "content": "林澈看了一眼手表。她留在岸边，将救生绳交给水手，协助救回落水者。"}],
            }
        return model_cls.model_validate(payload)


class NoPaidMedia:
    def __getattr__(self, name):
        raise AssertionError(f"Paid/media operation unexpectedly requested: {name}")


class DisabledReviewer:
    enabled = False


def events(response):
    result = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            result.append(json.loads(line[6:]))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PROJECT / "docs/evidence/RAG/pipeline-result.json")
    args = parser.parse_args()
    result = {
        "started_at": time.time(), "status": "running", "assertions": [],
        "verification_boundary": {
            "real": ["FastAPI authentication and owner checks", "multipart parsing", "SQLite persistence", "BAAI/bge-small-zh-v1.5 embeddings", "Qdrant on-disk filtering and retrieval", "durable execution and SSE", "document revisions and saved citations"],
            "substituted": ["StubLLM fixed script/storyboard responses; these do not establish generation quality"],
            "paid_calls": 0, "user_business_data_touched": False,
            "model_cache": str(MODEL_CACHE),
        },
    }

    def check(name, condition, detail=None):
        item = {"name": name, "passed": bool(condition)}
        if detail is not None:
            item["detail"] = detail
        result["assertions"].append(item)
        if not condition:
            raise AssertionError(name)

    runtime = PROJECT / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    try:
        check("real_model_cache_exists", MODEL_CACHE.exists())
        with tempfile.TemporaryDirectory(prefix="rag-pipeline-", dir=runtime) as temporary:
            root = Path(temporary)
            paths = {"DATA_DIR": root / "data", "SESSIONS_DIR": root / "data/sessions", "RESULT_DIR": root / "data/result",
                     "IMAGE_DIR": root / "data/result/image", "VIDEO_DIR": root / "data/result/video",
                     "SCRIPT_DIR": root / "data/result/script", "UPLOAD_DIR": root / "data/result/uploads"}
            for attr, path in paths.items():
                path.mkdir(parents=True, exist_ok=True)
                setattr(config, attr, path)

            from fastapi.testclient import TestClient
            from app.main import app
            from app.api.deps import get_orchestrator
            from app.services import auth, knowledge_retrieval, session_store
            from app.services.orchestrator import Orchestrator

            llm = StubLLM()
            orch = Orchestrator(llm=llm, image=NoPaidMedia(), video=NoPaidMedia(), tts=NoPaidMedia(), reviewer=DisabledReviewer())
            app.dependency_overrides[get_orchestrator] = lambda: orch
            try:
                with TestClient(app) as client:
                    check("unauthenticated_knowledge_request_rejected", client.get("/api/knowledge/libraries").status_code == 401)
                    headers = []
                    for code in auth.generate_invite_codes(2):
                        response = client.post("/api/auth/login", json={"invite_code": code})
                        check("temporary_account_login", response.status_code == 200)
                        headers.append({"Authorization": "Bearer " + response.json()["token"]})
                    own, other = headers
                    response = client.post("/api/knowledge/libraries", json={"name": "夜航 · 全链路验收资料", "description": "隔离验收样例，临时目录清理后不保留业务数据。"}, headers=own)
                    check("create_library_api", response.status_code == 201)
                    library = response.json()
                    library_id = library["library_id"]
                    docs = []
                    for title, text, constraint in (
                        ("林澈人物设定", "林澈是一位钟表修理师，她不会游泳。她住在港口钟楼，平日修理手表，救援时留在岸边把救生绳交给水手。", False),
                        ("人物固定约束", "林澈行动前必须用手表确认时间。不得让她拥有读心能力。", True),
                    ):
                        response = client.post(f"/api/knowledge/libraries/{library_id}/documents", files={"file": (title + ".md", text.encode(), "text/markdown")}, data={"title": title, "category": "character", "is_constraint": str(constraint).lower()}, headers=own)
                        check("upload_constraint" if constraint else "upload_role_document", response.status_code == 201)
                        docs.append(response.json())
                    result["uploaded_documents"] = docs
                    check("cross_owner_document_read_rejected", client.get(f'/api/knowledge/documents/{docs[0]["document_id"]}', headers=other).status_code == 404)
                    check("cross_owner_library_search_rejected", client.post(f"/api/knowledge/libraries/{library_id}/search", json={"query": "林澈"}, headers=other).status_code == 404)

                    query = "钟表修理师林澈不会游泳，在港口钟楼附近协助救援时留在岸边把救生绳交给水手。"
                    preview = client.post(f"/api/knowledge/libraries/{library_id}/search", json={"query": query}, headers=own)
                    check("real_semantic_preview_api", preview.status_code == 200)
                    result["retrieval_preview"] = preview.json()
                    check("normal_document_retrieved_by_real_embedding", docs[0]["document_id"] in {s["document_id"] for s in preview.json()["sources"]})

                    response = client.post("/api/sessions", json={"idea": query, "episodes": 1, "style": "realistic", "knowledge_library_ids": [library_id]}, headers=own)
                    check("create_bound_session_api", response.status_code == 200)
                    sid = response.json()["session_id"]
                    check("cross_owner_session_read_rejected", client.get(f"/api/sessions/{sid}", headers=other).status_code == 404)
                    traces = {}
                    for stage in ("script_generation", "storyboard"):
                        response = client.post(f"/api/sessions/{sid}/execute/{stage}", headers=own)
                        stage_events = events(response)
                        traces[stage] = [{"type": x.get("type"), "stage": x.get("stage"), "message": x.get("message")} for x in stage_events]
                        check(stage + "_sse_done", response.status_code == 200 and stage_events and stage_events[-1].get("type") == "done", [x.get("type") for x in stage_events])
                        current = client.get(f"/api/sessions/{sid}", headers=own).json()
                        source_ids = {s["document_id"] for s in current["artifacts"][stage]["knowledge_context"]["sources"]}
                        check(stage + "_source_snapshots_saved", {d["document_id"] for d in docs}.issubset(source_ids))
                    result["sse_traces"] = traces
                    result["llm_delivery_checks"] = llm.calls
                    check("both_stage_prompts_receive_real_sources_and_boundary", len(llm.calls) == 2 and all(x["has_constraint"] and x["has_role_fact"] and x["has_untrusted_data_boundary"] for x in llm.calls))
                    fresh = client.get(f"/api/sessions/{sid}", headers=own).json()
                    result["session_before_update"] = copy.deepcopy(fresh)
                    original_sources = copy.deepcopy(fresh["artifacts"]["script_generation"]["knowledge_context"]["sources"])
                    loaded = session_store.load_session(sid)
                    check("reload_persisted_sources", loaded.artifacts["script_generation"]["knowledge_context"]["sources"] == original_sources)
                    result["document_before_update"] = client.get(f'/api/knowledge/documents/{docs[0]["document_id"]}', headers=own).json()

                    response = client.post(f'/api/knowledge/documents/{docs[0]["document_id"]}/versions', files={"file": ("人物设定-v2.md", "林澈是一位钟表修理师，不会游泳。她的新工作室搬到了港口钟楼的二楼。".encode(), "text/markdown")}, headers=own)
                    check("update_document_version_api", response.status_code == 201 and response.json()["version_number"] == 2)
                    changed = client.get(f"/api/sessions/{sid}", headers=own).json()
                    check("session_reports_knowledge_changes", changed["knowledge_status"]["changed"] is True and bool(changed["knowledge_status"]["warnings"]))
                    check("document_update_preserves_generated_source_version", changed["artifacts"]["script_generation"]["knowledge_context"]["sources"] == original_sources)
                    result["session_after_update"] = copy.deepcopy(changed)
                    result["document_after_update"] = client.get(f'/api/knowledge/documents/{docs[0]["document_id"]}', headers=own).json()

                    edited = copy.deepcopy(changed["artifacts"]["script_generation"])
                    edited["title"] = "夜航 · 手动润色验收样例（生成器替身）"
                    edited["knowledge_context"] = {"sources": [{"text": "client-forged-source-must-be-rejected"}]}
                    response = client.post(f"/api/sessions/{sid}/intervene", json={"stage": "script_generation", "modifications": {"operation": "save", "artifact": edited}}, headers=own)
                    save_events = events(response)
                    check("save_script_sse_done", bool(save_events) and save_events[-1].get("type") == "done")
                    saved = client.get(f"/api/sessions/{sid}", headers=own).json()
                    saved_context = saved["artifacts"]["script_generation"]["knowledge_context"]
                    check("save_rejects_forged_sources_and_retains_originals", saved_context["sources"] == original_sources)
                    check("manual_edit_source_warning_is_persisted", saved_context.get("edited_since_generation") is True)
                    check("manual_save_makes_no_extra_llm_call", len(llm.calls) == 2)
                    result["session_after_manual_edit"] = copy.deepcopy(saved)

                    for document in docs:
                        response = client.delete(f'/api/knowledge/documents/{document["document_id"]}', headers=own)
                        check("delete_document_api", response.status_code == 200)
                    response = client.post(f"/api/knowledge/libraries/{library_id}/search", json={"query": query}, headers=own)
                    check("deleted_documents_are_absent_from_new_search", response.status_code == 200 and response.json()["status"] == "no_match" and response.json()["sources"] == [])
                    after_delete = client.get(f"/api/sessions/{sid}", headers=own).json()
                    check("old_source_snapshots_survive_document_deletion", after_delete["artifacts"]["script_generation"]["knowledge_context"]["sources"] == original_sources)
                    result["session_after_delete"] = after_delete
                    result["search_after_delete"] = response.json()
                    result["library"] = library
                    result["status"] = "passed"
            finally:
                app.dependency_overrides.pop(get_orchestrator, None)
                knowledge_retrieval.close()
        check("temporary_business_data_removed", not root.exists())
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        result["finished_at"] = time.time()
        result["passed"] = sum(item["passed"] for item in result["assertions"])
        result["total"] = len(result["assertions"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "passed": result["passed"], "total": result["total"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
