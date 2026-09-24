# -*- coding: utf-8 -*-
"""一次性迁移：旧 JSON 元数据 → SQLite。迁移后旧 JSON 移到 data/backup_json/ 保留（不删除）。"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core import config  # noqa: E402
from app.services import db  # noqa: E402
from app.schemas.session import SessionMeta  # noqa: E402
from app.schemas.task import TaskMeta  # noqa: E402


def migrate_sessions() -> int:
    count = 0
    for f in config.SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data.pop("schema_version", None)
            from app.services import session_store
            session_store.save_session(SessionMeta.model_validate(data))
            count += 1
        except Exception as e:
            print(f"  跳过会话 {f.name}: {e}")
    return count


def migrate_tasks() -> int:
    count = 0
    tasks_dir = config.DATA_DIR / "tasks"
    if not tasks_dir.exists():
        return 0
    for f in tasks_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            from app.services import task_store
            task_store.save_task(TaskMeta.model_validate(data))
            count += 1
        except Exception as e:
            print(f"  跳过任务 {f.name}: {e}")
    return count


def _migrate_kv(filename: str, table: str, columns: list[str], key_transform=None) -> int:
    """迁移 {key: {…}} 形式的 JSON 到表。columns 为键之后的列名。"""
    path = config.DATA_DIR / filename
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    count = 0
    with db.connect() as conn:
        for key, entry in data.items():
            cols = ", ".join(columns)
            ph = ", ".join("?" for _ in columns)
            values = [entry[c] for c in columns[1:]]  # columns[0] 是主键（key）
            conn.execute(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({ph})", [key] + values)
            count += 1
    return count


def migrate_auth() -> dict:
    result = {
        "invite_codes": _migrate_kv("invite_codes.json", "invite_codes",
                                    ["code", "status", "used_by", "created_at", "used_at"]),
        "users": _migrate_kv("users.json", "users", ["user_id", "invite_code", "created_at"]),
        "auth_tokens": _migrate_kv("auth_tokens.json", "auth_tokens",
                                   ["token", "user_id", "created_at", "expires_at"]),
        "uploads": _migrate_kv("uploads.json", "uploads",
                               ["filename", "owner_id", "original_name", "created_at"]),
    }
    return result


def backup_json() -> None:
    backup = config.DATA_DIR / "backup_json"
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("invite_codes.json", "users.json", "auth_tokens.json", "uploads.json"):
        p = config.DATA_DIR / name
        if p.exists():
            shutil.move(str(p), str(backup / name))
    for sub in ("sessions", "tasks"):
        src = (config.SESSIONS_DIR if sub == "sessions" else config.DATA_DIR / "tasks")
        if not src.exists():
            continue
        dest = backup / sub
        dest.mkdir(parents=True, exist_ok=True)
        for f in src.glob("*.json"):
            shutil.move(str(f), str(dest / f.name))


def main() -> None:
    db.init_db()
    print("=== 迁移开始 ===", flush=True)
    n_s = migrate_sessions()
    n_t = migrate_tasks()
    n_a = migrate_auth()
    print(f"会话迁移: {n_s}", flush=True)
    print(f"任务迁移: {n_t}", flush=True)
    print(f"鉴权迁移: {n_a}", flush=True)
    # 校验行数
    with db.connect() as conn:
        for t in ("sessions", "tasks", "invite_codes", "users", "auth_tokens", "uploads"):
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()
            print(f"  DB {t}: {row['c']} 行", flush=True)
    backup_json()
    print("=== 完成，旧 JSON 已备份到 data/backup_json/ ===", flush=True)


if __name__ == "__main__":
    main()
