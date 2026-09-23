"""已忽略问题（挂 /ignored）：按模块分页签集中展示生效中的忽略记录，可就地恢复。

数据源：index.db 的 ignores 表（生效中 = restored_at IS NULL）。
恢复 = 标记 restored_at（不物理删，保留历史）；恢复后各模块列表/概览卡/复核/日报即时生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

from flask import Blueprint, redirect, render_template, request

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import ignores  # noqa: E402
from db import IndexDB  # noqa: E402
from modules import EXTRA_MODULES  # noqa: E402

ignored_bp = Blueprint("ignored", __name__)
DB_PATH = str(BASE_DIR / "index.db")
MODULE = next((m for m in EXTRA_MODULES if m["key"] == "ignored"), {"key": "ignored",
                                                                    "name": "已忽略问题"})

# 页签与首页「每日增量内容检查」同名同序（图片 OCR 检查 → 英文文档检查 → 链接健康检查）
# imgnorm 已从入口移除，页签也不再列（其忽略记录仍可在「全部」里看到）
TABS = [("all", "全部"), ("ocr", "图片 OCR 检查"), ("encheck", "英文文档检查"),
        ("linkcheck", "链接健康检查")]


def _client_ip() -> str:
    x = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "")
    return (x.split(",")[0].strip() or request.remote_addr or "")[:64]


def _rows(db, module_key: str | None) -> list[dict]:
    """生效中的忽略记录 + 展示所需信息（文档标题/URL、图片路径）。"""
    doc: dict[str, tuple] = {}
    try:
        for dk, url, title in db._conn.execute("SELECT doc_key, url, title FROM docs"):
            doc[dk] = (url or "", title or "")
    except Exception:
        pass
    out = []
    for r in db.list_ignores(module_key, active_only=True):
        mk = r["module_key"]
        it = dict(r)
        it["kind_label"] = ignores.kind_label(mk, r["kind"])
        it["module_label"] = ignores.MODULE_LABEL.get(mk, mk)
        it["target_label"] = ignores.TARGET_LABEL.get(mk, "目标")
        if mk == "linkcheck":
            it["url"] = r["target"]
        elif mk == "encheck":
            if (r["target"] or "").startswith("http"):
                # 逐条「中文链接」忽略：目标就是那条链接本身
                it["url"] = r["target"]
                it["title"] = ""
            else:
                u, t = doc.get(r["target"], ("", ""))
                it["url"] = u
                it["title"] = t or r["target"]
        else:                                   # ocr / imgnorm：图片
            it["image"] = r["target"]
            it["image_rel"] = r["target"].replace("data/", "", 1)
        out.append(it)
    return out


@ignored_bp.route("/ignored/")
def ignored_home():
    tab = request.args.get("tab") or "all"
    if tab not in dict(TABS):
        tab = "all"
    db = IndexDB(DB_PATH)
    try:
        all_rows = _rows(db, None)
        counts: dict = {}
        for r in all_rows:
            counts[r["module_key"]] = counts.get(r["module_key"], 0) + 1
        rows = all_rows if tab == "all" else [r for r in all_rows if r["module_key"] == tab]
    finally:
        db.close()
    return render_template("ignored.html", rows=rows, tab=tab, tabs=TABS,
                           counts=counts, total=len(all_rows),
                           module=MODULE,
                           has_reason=any(r.get("reason") for r in rows))


@ignored_bp.route("/ignored/restore", methods=["POST"])
def ignored_restore():
    iid = request.form.get("id", type=int)
    db = IndexDB(DB_PATH)
    try:
        if iid:
            db.restore_ignore(iid, ip=_client_ip())
    finally:
        db.close()
    return redirect(request.referrer or "/ignored/")


def register_ignored(app, db_path: str = None):
    """注册「已忽略问题」只读+恢复页。"""
    app.register_blueprint(ignored_bp)
    return ignored_bp
