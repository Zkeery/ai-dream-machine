#!/usr/bin/env python3
"""Read-only export of the isolated real acceptance run; never imports the app.

No provider/API request is made. SQLite is opened mode=ro/query_only and all
rows are read in one transaction. Only evidence files in the output directory
are written. Run again after pending stages finish to refresh the export.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile


STAGES = {
    "story": ["script_generation", "character_design", "storyboard", "reference_generation", "video_generation", "post_production"],
    "comic": ["script_generation", "character_design", "comic_storyboard", "comic_panels", "comic_audio", "comic_composition"],
}
ACTIVE = {"pending", "running"}
HELD = {"reserved", "uncertain"}


def parse(value, default=None):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ref(value):
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest() if value else None


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_text(value):
    """Only user-authored scope text is exported; raw provider errors never are."""
    text = str(value or "")
    text = re.sub(r"https?://\S+", "[redacted-url]", text)
    text = re.sub(r"(?i)(?:bearer\s+|sk-)[A-Za-z0-9._-]+", "[redacted-credential]", text)
    return text


def machine_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) else None


def rows(conn, sql, args=()):
    return [dict(row) for row in conn.execute(sql, args)]


def cny(micros):
    return round(int(micros or 0) / 1_000_000, 6)


def ledger_summary(ledger):
    reported = [row["provider_paid_micros"] for row in ledger if row["provider_paid_micros"] is not None]
    return {
        "calls": len(ledger), "status_counts": dict(Counter(row["status"] for row in ledger)),
        "reserved_cny": cny(sum(row["reserved_micros"] for row in ledger if row["status"] in HELD)),
        "calculated_cny": cny(sum(row["charged_micros"] for row in ledger if row["status"] == "completed")),
        "provider_reported_cny": cny(sum(reported)) if reported else None,
        "provider_reported_calls": len(reported),
        "provider_reported_complete": bool(ledger) and len(reported) == len(ledger),
        "rejected_reservation_released_cny": cny(sum(row["reserved_micros"] for row in ledger if row["status"] == "rejected")),
        "accounting_notice": "按价格表估算；未知结果保留预留。供应商实付未提供时为 null，预留释放不代表退款。",
    }


def scope(artifact):
    if not isinstance(artifact, dict):
        return None
    result = {}
    for collection, id_field in (("characters", "character_id"), ("settings", "setting_id")):
        if collection in artifact:
            result[collection] = [{"id": safe_text(item.get(id_field)), "name": safe_text(item.get("name"))} for item in artifact[collection]]
            result[collection + "_count"] = len(artifact[collection])
    for collection in ("episodes", "shots", "segments"):
        if isinstance(artifact.get(collection), list):
            result[collection + "_count"] = len(artifact[collection])
    if isinstance(artifact.get("shots"), list):
        result["shot_ids"] = [safe_text(item.get("shot_id")) for item in artifact["shots"]]
        lines = [line for shot in artifact["shots"] for line in shot.get("dialogues", [])]
        if lines:
            result["dialogues"] = [{"line_id": safe_text(line.get("line_id")), "speaker_id": safe_text(line.get("speaker_id")), "text": safe_text(line.get("text"))} for line in lines]
    return result or None


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
        "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,pix_fmt,duration,r_frame_rate,avg_frame_rate,nb_frames,sample_rate,channels",
        "-of", "json", str(path)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def contact_sheet(video, output, duration):
    from PIL import Image, ImageDraw
    times = [round(duration * ratio, 3) for ratio in (0.1, 0.35, 0.6, 0.85)]
    width, height, caption = 480, 300, 26
    canvas = Image.new("RGB", (width * 2, (height + caption) * 2), "#10181e")
    draw = ImageDraw.Draw(canvas)
    with tempfile.TemporaryDirectory(prefix="acceptance-frames-") as directory:
        for index, timestamp in enumerate(times):
            frame = Path(directory) / f"frame-{index}.png"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", str(timestamp), "-i", str(video),
                "-frames:v", "1", "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x10181e", str(frame)],
                capture_output=True, timeout=45, check=True)
            x, y = index % 2 * width, index // 2 * (height + caption)
            with Image.open(frame) as image:
                canvas.paste(image.convert("RGB"), (x, y))
            draw.text((x + 12, y + height + 7), f"Frame {index + 1}  |  {timestamp:.3f} s", fill="#e8edf0")
    canvas.save(output, "JPEG", quality=92)
    return {"path": output.name, "sample_times_seconds": times, "sha256": sha256(output), "method": "4 frames at 10%, 35%, 60%, 85%; exploratory visual evidence, not exhaustive video QA"}


def export_media(artifact, case_name, data_dir, evidence_dir):
    value = artifact.get("final_video") if isinstance(artifact, dict) else None
    result = {"status": "pending", "final_video": None, "quality_verdict": "NEED_REVIEW"}
    if not value:
        return result
    source = Path(value).resolve()
    if not source.is_relative_to(data_dir.resolve()) or not source.is_file():
        return {**result, "status": "source_unavailable", "error_code": "FINAL_MEDIA_OUTSIDE_DATA_OR_MISSING"}
    target = evidence_dir / f"{case_name}.mp4"
    try:
        # Atomic publication keeps the previous valid export if the copy fails.
        with tempfile.NamedTemporaryFile(dir=evidence_dir, suffix=".mp4", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            shutil.copyfile(source, temporary_path)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)
        result.update({"status": "exported", "final_video": target.name, "source_sha256": sha256(source), "sha256": sha256(target), "size_bytes": target.stat().st_size})
        result["ffprobe"] = probe(target)
        duration = float(result["ffprobe"].get("format", {}).get("duration") or 0)
        if duration > 0:
            result["contact_sheet"] = contact_sheet(target, evidence_dir / f"{case_name}-contact-sheet.jpg", duration)
        else:
            result["contact_sheet"] = {"status": "unavailable", "error_code": "NO_POSITIVE_DURATION"}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["verification_status"] = "incomplete"
        result["error_code"] = type(error).__name__
    return result


def case_export(case_name, session, executions, requests, jobs, ledger, run, data_dir, evidence_dir):
    artifacts = parse(session["artifacts"], {})
    versions = parse(session.get("artifact_versions"), {})
    request_map = {row["execution_id"]: row for row in requests}
    event_map = {item["execution_id"]: item for item in run.get("events", []) if item.get("execution_id")}
    # Some orchestrator versions only append timings under cases[].stages.
    event_map.update({item["execution_id"]: item for item in run["cases"][case_name].get("stages", []) if item.get("execution_id")})
    execution_details = []
    scope_changes = []
    for execution in executions:
        execution_id = execution["execution_id"]
        request = request_map.get(execution_id, {})
        event = event_map.get(execution_id, {})
        timing = event.get("elapsed_seconds")
        timing_source = "run_record_wall_clock" if isinstance(timing, (int, float)) else "execution_updated_minus_created"
        if timing is None:
            timing = max(0, execution["updated_at"] - execution["created_at"])
        model_usage = parse(request.get("model_usage"), {}) or {}
        models = {value for value in model_usage.get("models", {}).values() if value}
        models.update(row["model"] for row in ledger if row["execution_id"] == execution_id)
        execution_details.append({"execution_ref": ref(execution_id), "stage": execution["stage"], "operation": execution["operation"],
            "source_execution_ref": ref(request.get("source_execution_id")), "status": execution["status"], "created_at": execution["created_at"],
            "updated_at": execution["updated_at"], "elapsed_seconds": round(timing, 3), "elapsed_basis": timing_source,
            "elapsed_is_final": execution["status"] not in ACTIVE, "models": sorted(models), "had_error": bool(execution["error"]),
            "ledger_call_count": sum(row["execution_id"] == execution_id for row in ledger)})
        payload = parse(request.get("payload"), {}) or {}
        if execution["operation"] in {"save", "select", "regenerate"} or event.get("operation") == "manual_scope_edit":
            preceding = [version for version in versions.get(execution["stage"], []) if version.get("created_at", 0) <= execution["created_at"]]
            preceding.sort(key=lambda version: version.get("created_at", 0))
            scope_changes.append({"execution_ref": ref(execution_id), "stage": execution["stage"], "operation": event.get("operation") or execution["operation"],
                "at": execution["created_at"], "status": execution["status"], "target_ids": [safe_text(x) for x in payload.get("target_ids", [])],
                "before_scope": scope(preceding[-1].get("artifact")) if preceding else None,
                "after_scope": scope(payload.get("artifact")), "payload_sha256": digest(payload),
                "note": "Manual save scope is from persisted request; targeted regeneration is not a new full run."})
    stages = []
    completed = set(parse(session["stages_completed"], []))
    stale = set(parse(session.get("stale_stages"), []))
    for stage in STAGES[case_name]:
        history = [item for item in execution_details if item["stage"] == stage]
        stages.append({"stage": stage, "current_status": "stale" if stage in stale else "completed" if stage in completed else history[-1]["status"] if history else "pending",
            "execution_count": len(history), "generation_execution_count": sum(item["operation"] not in {"save", "select"} for item in history),
            "elapsed_seconds_total": round(sum(item["elapsed_seconds"] for item in history), 3),
            "models": sorted({model for item in history for model in item["models"]}), "artifact_scope": scope(artifacts.get(stage)), "executions": history})
    provider = [{"job_ref": ref(job["id"]), "execution_ref": ref(job["execution_id"]), "scope_execution_ref": ref(job["scope_execution_id"]),
        "stage": job["stage"], "kind": job["kind"], "model": job["model"], "status": job["status"], "attempt": job.get("attempt") or 1,
        "retry_of_ref": ref(job.get("retry_of")), "http_status": job.get("http_status"), "has_rejection_evidence": bool(job.get("rejection_evidence")),
        "has_remote_job": bool(job["vendor_job_id"]), "has_result_url": bool(job["result_url"]), "has_local_result": bool(job["local_path"]),
        "result_sha256": job["result_sha256"], "error_code": machine_code(job["error_code"]), "created_at": job["created_at"], "updated_at": job["updated_at"]} for job in jobs]
    ledger_details = [{"call_ref": ref(row["call_id"]), "execution_ref": ref(row["execution_id"]), "model": row["model"], "kind": row["kind"],
        "month": row["month"], "status": row["status"], "reserved_units": {key: value for key, value in parse(row["units"], {}).items() if key in {"input_tokens", "output_tokens", "images", "seconds", "resolution"}},
        "price_snapshot_sha256": digest(parse(row["rates"], {})), "original_reservation_cny": cny(row["reserved_micros"]),
        "held_cny": cny(row["reserved_micros"]) if row["status"] in HELD else 0,
        "calculated_cny": cny(row["charged_micros"]), "provider_reported_cny": None if row["provider_paid_micros"] is None else cny(row["provider_paid_micros"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in ledger]
    final_stage = STAGES[case_name][-1]
    media = export_media(artifacts.get(final_stage, {}), case_name, data_dir, evidence_dir)
    return {"case": case_name, "session_ref": ref(session["session_id"]), "session_status": session["status"], "current_stage": session["current_stage"],
        "original_request": {"idea": safe_text(session["idea"]), "episodes": session["episodes"], "video_ratio": session["video_ratio"], "resolution": session["resolution"], "style": safe_text(session["style"]),
            "models": {key: value for key, value in parse(session.get("model_selection"), {}).items() if key in {"text", "image", "video_first_frame", "video_start_end", "video_reference"}}},
        "stage_count": len(stages), "stages": stages, "execution_count": len(executions), "provider_jobs": provider,
        "ledger": {"summary": ledger_summary(ledger), "calls": ledger_details}, "scope_changes": scope_changes,
        "generated_scope_history": {stage: [{"version_ref": ref(version.get("version_id")), "created_at": version.get("created_at"), "reason": safe_text(version.get("reason")),
            "scope": scope(version.get("artifact"))} for version in history] for stage, history in versions.items() if stage in {"script_generation", "storyboard", "comic_storyboard", "character_design"}},
        "media": media, "quality_review": {"status": "NEED_REVIEW", "reviewer": None, "reviewed_at": None,
            "rules": {rule: None for rule in ("Q01", "Q02", "Q03", "Q04", "Q05")}, "notes": "人工画面、连续运动及声音验收待填写；机器导出不产生质量通过结论。"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    root = args.project_root.resolve()
    data_dir = (args.data_dir or root / ".runtime/real-acceptance-20261004").resolve()
    evidence_dir = (args.evidence_dir or root / "docs/evidence/真实样片验收").resolve()
    run_path = evidence_dir / "run.json"
    raw_run = run_path.read_bytes()
    run = json.loads(raw_run)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    old_result = parse((evidence_dir / "result.json").read_text()) if (evidence_dir / "result.json").exists() else {}
    snapshot = {}
    with sqlite3.connect((data_dir / "app.db").as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        for name in STAGES:
            session_id = run.get("cases", {}).get(name, {}).get("session_id")
            sessions = rows(conn, "SELECT * FROM sessions WHERE session_id=?", (session_id,))
            if not sessions:
                snapshot[name] = None
                continue
            executions = rows(conn, "SELECT * FROM executions WHERE entity_type='session' AND entity_id=? ORDER BY created_at,execution_id", (session_id,))
            requests = rows(conn, "SELECT r.* FROM execution_requests r JOIN executions e USING(execution_id) WHERE e.entity_type='session' AND e.entity_id=?", (session_id,))
            jobs = rows(conn, "SELECT j.*,a.retry_of,a.attempt,a.http_status,a.rejection_evidence FROM provider_jobs j LEFT JOIN provider_job_attempts a ON a.job_id=j.id WHERE j.entity_type='session' AND j.entity_id=? ORDER BY j.created_at,j.id", (session_id,))
            ledger = rows(conn, "SELECT l.* FROM model_cost_ledger l JOIN executions e USING(execution_id) WHERE e.entity_type='session' AND e.entity_id=? ORDER BY l.created_at,l.call_id", (session_id,))
            snapshot[name] = (sessions[0], executions, requests, jobs, ledger)
        conn.rollback()
    result = {"schema_version": 1, "run_id": safe_text(run.get("run_id")), "exported_at": datetime.now(timezone.utc).isoformat(),
        "sources": {"database": "app.db", "database_scope": "isolated_acceptance_data", "database_read_mode": "mode=ro; query_only; single read transaction", "run_file": "run.json", "run_file_sha256": hashlib.sha256(raw_run).hexdigest(), "run_updated_at": run.get("updated_at")},
        "paid_provider_calls": bool(run.get("paid_provider_calls")), "cases": {}, "quality_verdict": "NEED_REVIEW",
        "limitations": ["两例探索性真实验收，不推断总体稳定性或一次通过率。", "执行耗时取实测记录；缺记录时使用执行更新时间减创建时间，不代表精确供应商处理耗时。", "四帧只用于定位画面，不能代替连续播放和听审。", "供应商实付无账单时保留 null。", "导出不会修改应用数据库、run.json 或调用模型；正在运行的阶段可以继续更新源数据。"]}
    all_ledger = []
    for name, data in snapshot.items():
        if data is None:
            result["cases"][name] = {"case": name, "status": "pending", "reason": "No recorded session", "quality_review": {"status": "NEED_REVIEW"}}
            continue
        case = case_export(name, *data, run, data_dir, evidence_dir)
        old = old_result.get("cases", {}).get(name, {})
        if case["media"].get("sha256") and old.get("media", {}).get("sha256") == case["media"]["sha256"] and old.get("quality_review", {}).get("reviewer"):
            case["quality_review"] = old["quality_review"]
        result["cases"][name] = case
        all_ledger.extend(data[-1])
    result["ledger_total"] = ledger_summary(all_ledger)
    by_model = defaultdict(list)
    for row in all_ledger:
        by_model[(row["model"], row["kind"])].append(row)
    result["ledger_by_model"] = [{"model": model, "kind": kind, **ledger_summary(group)} for (model, kind), group in sorted(by_model.items())]
    result["run_events"] = [{key: safe_text(event[key]) if key in {"case", "stage", "operation", "status"} else event[key] for key in ("case", "stage", "operation", "status", "at", "elapsed_seconds") if key in event} | {"execution_ref": ref(event.get("execution_id")), "source_execution_ref": ref(event.get("source_execution_id")), "had_error": bool(event.get("error"))} for event in run.get("events", [])]
    result["source_code_versions"] = []
    for relative, initial in sorted(run.get("initial_source_versions", {}).items()):
        path = (root / relative).resolve()
        if not path.is_relative_to(root / "backend/app") or path.suffix != ".py":
            continue
        current = sha256(path) if path.is_file() else None
        result["source_code_versions"].append({"path": relative, "initial_sha256": initial, "latest_recorded_sha256": run.get("latest_source_versions", {}).get(relative), "export_time_sha256": current, "changed_since_start": current != initial})
    manifest = []
    for path in sorted(evidence_dir.iterdir()):
        if path.is_file() and path.name != "result.json" and (path.suffix.lower() in {".mp4", ".jpg", ".png", ".srt", ".md"} or path.name in {"run.json", "story-board-edited.json", "comic-board-edited.json", "comic-setting-regenerate.json", "工程验证.json"}):
            manifest.append({"path": path.name, "sha256": hashlib.sha256(raw_run).hexdigest() if path == run_path else sha256(path), "size_bytes": len(raw_run) if path == run_path else path.stat().st_size})
    result["file_manifest"] = manifest
    result["public_evidence_sha256"] = digest(result["cases"])
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    forbidden = [secret for data in snapshot.values() if data for job in data[3] for secret in (job["owner_id"], job["vendor_job_id"], job["result_url"]) if secret]
    if any(secret in encoded for secret in forbidden) or re.search(r"https?://|Bearer\s|sk-[A-Za-z0-9]{12,}", encoded):
        raise RuntimeError("Evidence redaction check failed; no result was written")
    result["redaction_check"] = {"passed": True, "checked": ["no raw vendor job IDs", "no provider URLs", "no credential-shaped values", "no owner IDs", "no raw provider errors"]}
    output = evidence_dir / "result.json"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=evidence_dir, suffix=".json", delete=False) as stream:
        temp_path = Path(stream.name)
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temp_path.replace(output)
    print(json.dumps({"result": str(output), "cases": {name: item.get("media", {}).get("status", item.get("status")) for name, item in result["cases"].items()}, "ledger_calls": result["ledger_total"]["calls"], "redaction_passed": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
