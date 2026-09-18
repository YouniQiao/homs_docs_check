"""A2A 记录只读页（挂 /a2a）：展示 Hermes A2A 平台的往来消息与会话上下文。

数据源（只读）：
  ~/.hermes/a2a_audit.jsonl            逐条 inbound/outbound 记录
  ~/.hermes/a2a_conversations/*.jsonl  各会话上下文（user/agent 交替）

防搜索引擎收录：本蓝图所有响应加 X-Robots-Tag: noindex。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from flask import Blueprint, render_template, request

HERMES_HOME = Path.home() / ".hermes"
AUDIT_PATH = HERMES_HOME / "a2a_audit.jsonl"
CONV_DIR = HERMES_HOME / "a2a_conversations"

a2a_bp = Blueprint("a2a", __name__)


def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _load_audit() -> list[dict]:
    rows = _load_jsonl(AUDIT_PATH)
    for r in rows:
        r["ts_h"] = _fmt_ts(r.get("ts"))
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows


def _load_convs() -> list[dict]:
    convs: list[dict] = []
    if not CONV_DIR.is_dir():
        return convs
    for f in CONV_DIR.glob("*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        msgs = _load_jsonl(f)
        for m in msgs:
            m["ts_h"] = _fmt_ts(m.get("ts"))
        convs.append({
            "name": f.stem,
            "mtime": mtime,
            "mtime_h": _fmt_ts(mtime),
            "msgs": msgs,
        })
    convs.sort(key=lambda c: c["mtime"], reverse=True)
    return convs


def register_a2a(app, db_path: str = ""):
    @a2a_bp.after_request
    def _noindex(resp):
        # 防搜索引擎收录（内容含与外部 agent 的往来对话）
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return resp

    @a2a_bp.route("/a2a", strict_slashes=False)
    def a2a_page():
        audit = _load_audit()

        direction = (request.args.get("direction") or "").strip()
        peer = (request.args.get("peer") or "").strip()

        peers = sorted({r.get("peer") for r in audit if r.get("peer")})

        rows = audit
        if direction in ("inbound", "outbound"):
            rows = [r for r in rows if r.get("direction") == direction]
        if peer:
            rows = [r for r in rows if r.get("peer") == peer]

        stats = {
            "total": len(audit),
            "inbound": sum(1 for r in audit if r.get("direction") == "inbound"),
            "outbound": sum(1 for r in audit if r.get("direction") == "outbound"),
            "peers": len(peers),
        }

        return render_template(
            "a2a.html",
            rows=rows,
            stats=stats,
            peers=peers,
            convs=_load_convs(),
            cur_direction=direction,
            cur_peer=peer,
        )

    app.register_blueprint(a2a_bp)
    return a2a_bp
