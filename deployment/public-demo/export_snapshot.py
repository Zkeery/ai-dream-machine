"""Export only the two approved recording works, without importing the app.

Run with the project's Python. The source database is opened read-only. No
account, token, execution input, provider configuration or database is shipped.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / ".runtime/release-demo/data"
STAGES = ["script_generation", "character_design", "storyboard", "reference_generation", "video_generation", "post_production"]
FIELDS = ["session_id", "idea", "project_type", "style", "episodes", "video_ratio", "resolution", "expand_idea", "video_generation_mode", "status", "current_stage", "created_at", "updated_at"]
JSON_FIELDS = ["stages_completed", "artifacts", "artifact_versions", "selected_versions", "stale_stages"]
OMIT = {"model_usage", "model_selection", "owner_id", "execution_inputs", "content_reviews", "agent_runs", "knowledge_context", "parent_execution_id"}


def content_key(artifact):
    content = {k: v for k, v in artifact.items() if k != "model_usage"}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def export(destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    pair = json.loads((ROOT / ".runtime/release-demo/recording-pair.json").read_text())
    conn = sqlite3.connect(f"file:{DATA / 'app.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    media = set()

    def clean(value, sid):
        if isinstance(value, dict):
            return {k: clean(v, sid) for k, v in value.items() if k not in OMIT}
        if isinstance(value, list):
            return [clean(v, sid) for v in value]
        if isinstance(value, str) and value.startswith(str(DATA / "result") + "/"):
            source = Path(value).resolve()
            relative = source.relative_to((DATA / "result").resolve())
            if len(relative.parts) != 3 or relative.parts[0] not in {"image", "video", "script"} or relative.parts[1] != sid:
                raise ValueError("Unexpected media scope")
            if not source.is_file() or source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".mp4", ".wav", ".mp3"}:
                raise ValueError("Missing or unsupported media")
            target_relative = relative.with_suffix(".jpg") if source.suffix.lower() in {".png", ".jpg", ".jpeg"} else relative
            target = destination / "media" / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                # Browser delivery copies only: preserve original recording files.
                if target.suffix == ".jpg":
                    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(source), "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "3", str(target)], check=True)
                elif target.suffix == ".mp4":
                    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(source), "-c:v", "libx264", "-preset", "medium", "-crf", "27", "-maxrate", "650k", "-bufsize", "1300k", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(target)], check=True)
                else: shutil.copyfile(source, target)
            media.add(str(target_relative))
            return "/media/" + str(target_relative)
        return value

    sessions = []
    for name in ("complete", "resume"):
        sid = pair[name]
        row = dict(conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone())
        session = {k: row[k] for k in FIELDS}
        session.update({k: json.loads(row[k]) for k in JSON_FIELDS})
        assert session["artifacts"]["script_generation"]["title"].startswith("雨夜归途")
        session["artifact_versions"] = {k: [v for v in vs if v.get("reason") in {"generated", "legacy"}] for k, vs in session["artifact_versions"].items()}
        session["creation_checks"] = []
        for report in json.loads(row["creation_checks"]):
            safe = {k: report[k] for k in ("report_id", "stage", "version_id", "created_at", "status", "findings")}
            artifact = session["artifacts"].get(report["stage"])
            safe["matches_current"] = bool(artifact and content_key(artifact) == report["content_sha256"] and (report["version_id"] is None or session["selected_versions"].get(report["stage"]) == report["version_id"]))
            session["creation_checks"].append(safe)
        session.update(stage_order=STAGES, model_selection={}, knowledge_library_ids=[], error=None, execution=None)
        sessions.append(clean(session, sid))
    conn.close()
    payload = json.dumps(sessions, ensure_ascii=False, indent=2)
    for forbidden in ("localhost", "127.0.0.1", "/Users/", "Mock", "mock", "测试", "Traceback"):
        if forbidden in payload: raise ValueError(f"Private/development text in snapshot: {forbidden}")
    (destination / "sessions.json").write_text(payload)
    (destination / "media.json").write_text(json.dumps(sorted(media)))
    print(json.dumps({"sessions": len(sessions), "media": len(media), "destination": str(destination)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination")
    export(parser.parse_args().destination)
