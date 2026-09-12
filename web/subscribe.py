"""邮件订阅蓝图：订阅 / 退订表单。

路由：
  /subscribe        GET 显示表单；POST 订阅或退订
"""

from __future__ import annotations

import re

from flask import Blueprint, render_template, request

from db import IndexDB

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

sub_bp = Blueprint("subscribe", __name__)


def _render(email: str = "", msg: str = "", count: int = 0):
    return render_template("subscribe.html", email=email, msg=msg, count=count)


def register_subscribe(app, db_path: str):
    @sub_bp.route("/subscribe", methods=["GET", "POST"], strict_slashes=False)
    def subscribe():
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            action = request.form.get("action", "subscribe")
            if not _EMAIL_RE.match(email):
                return _render(email, "⚠️ 邮箱地址格式不正确")
            db = IndexDB(db_path)
            try:
                if action == "subscribe":
                    new = db.add_subscriber(email)
                    msg = ("✅ 订阅成功！每天会自动收到文档检查结果。"
                           if new else "ℹ️ 该邮箱已在订阅中。")
                else:
                    ok = db.remove_subscriber(email)
                    msg = ("✅ 已退订，需要时可随时重新订阅。"
                           if ok else "⚠️ 该邮箱未在订阅中。")
                count = db.count_subscribers()
            finally:
                db.close()
            return _render(email, msg, count)

        db = IndexDB(db_path)
        try:
            count = db.count_subscribers()
        finally:
            db.close()
        return _render(count=count)

    app.register_blueprint(sub_bp)
    return sub_bp