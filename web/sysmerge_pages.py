"""系统合并整改 — 关键词配置页（/sysmerge/keywords）。

用户要求：待识别关键词/链接做成**页面可编辑**，以后自己加减，不用改代码。
保存后即时生效（词表 + 忽略判定的 kind 列表都会刷新）。
"""

from __future__ import annotations

import json
import pathlib

from flask import Blueprint, redirect, render_template, request

ROOT = pathlib.Path(__file__).resolve().parent.parent
KW_FILE = ROOT / "sysmerge" / "keywords.json"


def _read() -> dict:
    try:
        return json.loads(KW_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"terms": [], "links": []}


def _write(terms: list[str], links: list[str]) -> None:
    KW_FILE.write_text(
        json.dumps({"terms": terms, "links": links}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def register_sysmerge(app, db_path: str = ""):
    bp = Blueprint("sysmerge_pages", __name__)

    @bp.route("/sysmerge/keywords", methods=["GET", "POST"])
    def sysmerge_keywords():
        saved = request.args.get("saved")
        if request.method == "POST":
            terms = [ln.strip() for ln in (request.form.get("terms") or "").splitlines()
                     if ln.strip()]
            links = [ln.strip() for ln in (request.form.get("links") or "").splitlines()
                     if ln.strip()]
            _write(terms, links)
            # 即时刷新忽略判定的 kind 列表（否则要重启才认新词）
            try:
                import ignores
                ignores.KINDS["sysmerge"] = [(t, t, None, None) for t in (terms + links)]
            except Exception:
                pass
            return redirect("/sysmerge/keywords?saved=1")
        d = _read()
        return render_template("sysmerge_keywords.html",
                               terms="\n".join(d.get("terms", [])),
                               links="\n".join(d.get("links", [])),
                               saved=saved)

    app.register_blueprint(bp)
    return bp
