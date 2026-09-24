# -*- coding: utf-8 -*-
"""旧会话归属迁移：把无 owner_id 的第一刀会话归属给指定用户。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core import config  # noqa: E402


def orphans() -> list[Path]:
    result = []
    for path in config.SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not data.get("owner_id"):
            result.append(path)
    return sorted(result)


def main() -> None:
    p = argparse.ArgumentParser(description="旧会话归属迁移")
    p.add_argument("--list-orphans", action="store_true", help="列出无归属会话")
    p.add_argument("--assign-to", help="归属到指定 user_id")
    p.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run）")
    args = p.parse_args()

    paths = orphans()
    if args.list_orphans or not args.assign_to:
        print(f"无归属会话 {len(paths)} 个：")
        for path in paths:
            print(f"  {path.stem}")
        if not args.assign_to:
            return

    if not args.assign_to:
        return
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["owner_id"] = args.assign_to
        if args.apply:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  已归属 {path.stem} -> {args.assign_to}")
        else:
            print(f"  [dry-run] {path.stem} -> {args.assign_to}")
    if not args.apply:
        print("（dry-run，未写入；加 --apply 才生效）")


if __name__ == "__main__":
    main()
