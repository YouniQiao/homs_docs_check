"""自签 /me 登录态 session cookie（读 .auth.env 的 FLASK_SECRET_KEY，不回显密钥）。

用法：
  python3 scripts/me_make_cookie.py [user_id] [login] [gitcode_id] > /tmp/me_cookie.txt
只写 cookie 值到 stdout（不含密钥），供 curl -H "Cookie: session=<值>" 使用。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
WEB = BASE / "web"
for p in (str(WEB), str(BASE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app import app  # noqa: E402  （导入即建 app；不会起服务）

uid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
login = sys.argv[2] if len(sys.argv) > 2 else "hhxi"
gid = sys.argv[3] if len(sys.argv) > 3 else ""


def main() -> int:
    si = app.session_interface
    ser = si.get_signing_serializer(app)
    if ser is None:
        print("NO_SECRET_KEY", file=sys.stderr)
        return 2
    data = {"user_id": uid, "gitcode_id": gid, "login": login,
            "name": login, "avatar": ""}
    sys.stdout.write(ser.dumps(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
