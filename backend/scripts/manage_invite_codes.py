# -*- coding: utf-8 -*-
"""邀请码管理（本地运维，不发 HTTP）：生成 / 列表 / 作废。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services import auth  # noqa: E402


def cmd_generate(args: argparse.Namespace) -> None:
    codes = auth.generate_invite_codes(args.n)
    print(f"已生成 {len(codes)} 个邀请码：")
    for c in codes:
        print(f"  {c}")


def cmd_list(_args: argparse.Namespace) -> None:
    codes = auth.list_invite_codes()
    if not codes:
        print("（暂无邀请码）")
        return
    for e in codes:
        print(f"{e['code']}  {e['status']}  used_by={e['used_by'] or '-'}")


def cmd_revoke(args: argparse.Namespace) -> None:
    auth.revoke_invite_code(args.code)
    print(f"已作废：{args.code.upper()}")


def main() -> None:
    p = argparse.ArgumentParser(description="邀请码管理")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="生成邀请码")
    g.add_argument("-n", type=int, default=5)
    g.set_defaults(func=cmd_generate)
    l = sub.add_parser("list", help="列出邀请码")
    l.set_defaults(func=cmd_list)
    r = sub.add_parser("revoke", help="作废未使用邀请码")
    r.add_argument("code")
    r.set_defaults(func=cmd_revoke)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
