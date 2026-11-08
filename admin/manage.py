"""管理员账号维护 CLI。

用法::

    python -m admin.manage list
    python -m admin.manage create-admin <用户名> [--password <密码>]
    python -m admin.manage reset-password <用户名> [--password <密码>]
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .config import AdminSettings
from .db import AdminDB
from .security import hash_password


def _password(value: str | None, prompt: str) -> str:
    if value:
        password = value
    else:
        password = getpass.getpass(prompt)
    if len(password) < 8:
        print("密码至少 8 位。", file=sys.stderr)
        raise SystemExit(2)
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TFD 后台管理员账号维护")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出管理员")

    create = sub.add_parser("create-admin", help="创建管理员")
    create.add_argument("username")
    create.add_argument("--password")
    create.add_argument("--role", default="super_admin")

    reset = sub.add_parser("reset-password", help="重置密码并踢出会话")
    reset.add_argument("username")
    reset.add_argument("--password")

    args = parser.parse_args(argv)
    settings = AdminSettings()
    db = AdminDB(settings.admin_db)
    try:
        if args.command == "list":
            rows = db._conn.execute(
                "SELECT id, username, role, disabled, created_at, last_login_at "
                "FROM admin_users ORDER BY id"
            ).fetchall()
            for row in rows:
                print(dict(row))
            return 0

        if args.command == "create-admin":
            if db.get_admin_by_username(args.username):
                print("用户名已存在。", file=sys.stderr)
                return 2
            password = _password(args.password, "新管理员密码：")
            admin_id = db.create_admin(
                args.username, hash_password(password), role=args.role
            )
            print(f"已创建管理员 #{admin_id} {args.username}（{args.role}）")
            return 0

        if args.command == "reset-password":
            row = db.get_admin_by_username(args.username)
            if row is None:
                print("管理员不存在。", file=sys.stderr)
                return 2
            password = _password(args.password, "新密码：")
            db.update_password(int(row["id"]), hash_password(password))
            db.revoke_admin_sessions(int(row["id"]))
            print(f"已重置 {args.username} 的密码，并踢出所有会话。")
            return 0
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
