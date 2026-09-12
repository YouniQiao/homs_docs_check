"""问题反馈蓝图：提交表单 + 公开列表（任何人都可查看）。

路由：
  /feedback/          提交表单 + POST 入库
  /feedback/list      所有反馈列表（可按状态筛选）
  /feedback/<id>/<status>  更新某条状态（new/doing/done/ignore）
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from db import IndexDB

_STATUS_LABELS = {
    "new": "待处理",
    "doing": "处理中",
    "done": "已处理",
    "ignore": "已忽略",
}

feedback_bp = Blueprint("feedback", __name__)


def _render_form(db_path: str, msg: str = "", status: str = ""):
    db = IndexDB(db_path)
    try:
        items = db.list_feedbacks(status or None)
    finally:
        db.close()
    return render_template("feedback_submit.html", msg=msg, items=items,
                           cur=status, status_labels=_STATUS_LABELS)


# 工厂：因为 db_path 要在 create_app 时注入，暴露 register 函数
def register_feedback(app, db_path: str):
    bp = feedback_bp

    @bp.route("/feedback", methods=["GET", "POST"], strict_slashes=False)
    def submit():
        if request.method == "POST":
            content = request.form.get("content", "").strip()
            contact = request.form.get("contact", "").strip()
            if content:
                db = IndexDB(db_path)
                try:
                    db.create_feedback(content, contact)
                finally:
                    db.close()
                return _render_form(db_path, msg="✅ 已收到你的反馈，谢谢！")
            return _render_form(db_path, msg="⚠️ 反馈内容不能为空")
        return _render_form(db_path, status=request.args.get("status", ""))

    @bp.route("/feedback/<int:fid>/<status>", strict_slashes=False)
    def set_status(fid: int, status: str):
        if status in _STATUS_LABELS:
            db = IndexDB(db_path)
            try:
                db.update_feedback_status(fid, status)
            finally:
                db.close()
        return redirect(url_for("feedback.submit"))

    app.register_blueprint(bp)
    return bp
