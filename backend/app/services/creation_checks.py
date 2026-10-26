"""Mock-only product adapter for the frozen offline checker.

Reports are append-only session data, bound to content, sources and version.
Hints never approve a whole answer or replace a human judgment.
"""
from __future__ import annotations

import hashlib
import json
import time
from uuid import uuid4

from app.core import config
from app.services.answer_grounding_v13 import VERSION, check_answer_grounding

TEXT_STAGES = {"script_generation", "storyboard", "comic_storyboard"}
STAGES = {"script_generation": "剧本", "storyboard": "分镜", "comic_storyboard": "漫剧分镜"}
FIELDS = {"title": "标题", "logline": "故事梗概", "content": "剧情", "description": "描述",
          "prompt": "画面要求", "text": "对白", "role": "人物定位", "mood": "氛围"}
COLLECTIONS = {"characters": "人物", "settings": "场景", "episodes": "第{}集", "shots": "镜头{}", "dialogues": "对白{}"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def content_key(artifact):
    # Usage/cost metadata is not creative text. Knowledge snapshots remain bound.
    return digest({k: v for k, v in artifact.items() if k != "model_usage"})


def text_parts(value, location):
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key in FIELDS and isinstance(item, str) and item.strip():
            yield location + " · " + FIELDS[key], item
        elif key in COLLECTIONS and isinstance(item, list):
            for index, child in enumerate(item, 1):
                label = COLLECTIONS[key]
                label = label.format(index) if "{}" in label else f"{label}{index}"
                yield from text_parts(child, location + " · " + label)


def record_check(meta, stage, *, force=False):
    if not config.settings.mode_mock or stage not in TEXT_STAGES or not meta.artifacts.get(stage):
        return None
    artifact = meta.artifacts[stage]
    key = content_key(artifact)
    version = meta.selected_versions.get(stage)
    selected = next((v for v in meta.artifact_versions.get(stage, []) if v["version_id"] == version), None)
    if not selected or content_key(selected["artifact"]) != key:
        version = None  # Saved edits are not a successful generation version.
    old = next((r for r in reversed(meta.creation_checks) if r["stage"] == stage
                and r["content_sha256"] == key and r["version_id"] == version
                and r["checker_version"] == VERSION and r["status"] != "unavailable"), None)
    if old and not force:
        return old
    sources = (artifact.get("knowledge_context") or {}).get("sources") or []
    report = dict(report_id=uuid4().hex, stage=stage, version_id=version,
                  content_sha256=key, sources_sha256=digest(sources), checker_version=VERSION,
                  created_at=time.time(), mode="mock", findings=[], checks=[], status="limited")
    try:
        citations = [s.get("citation_id", s.get("id")) for s in sources]
        for location, text in text_parts(artifact, STAGES[stage]):
            result = check_answer_grounding(text, sources, citations)
            report["checks"].append(dict(location=location, result=result))
            if result["status"] == "NEED_REVIEW":
                report["findings"].append(dict(location=location, excerpt=text,
                    problem="这段话的因果、条件或保证，尚未找到完整的资料支持。",
                    suggestion="对照创作资料核实这段话；补齐条件或来源，或删除没有依据的保证，再保存检查。"))
        if report["findings"]:
            report["status"] = "review"
        elif any(c["result"]["status"] == "TEXT_EVIDENCE_FOUND" for c in report["checks"]):
            report["status"] = "evidence"
    except Exception:
        # A checker outage must not destroy a generated artifact or its version.
        report.update(status="unavailable", findings=[], checks=[])
    meta.creation_checks.append(report)
    return report


def public_checks(meta):
    history = []
    for report in meta.creation_checks:
        safe = {k: report[k] for k in ("report_id", "stage", "version_id", "created_at", "status", "findings")}
        artifact = meta.artifacts.get(report["stage"])
        safe["matches_current"] = bool(artifact and content_key(artifact) == report["content_sha256"]
            and (report["version_id"] is None or meta.selected_versions.get(report["stage"]) == report["version_id"]))
        history.append(safe)
    return history
