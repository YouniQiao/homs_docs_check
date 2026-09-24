"""模块：当前全量问题。

执行脚本：recheck/recheck.py（把历史发现的问题重新核一遍）。

页面形态：按模块分页签，每个页签**沿用该模块原来的列 / 徽标 / 筛选**
（直接复用 LINKCHECK/ENCHECK/OCR 三个模块的定义）；
明细**只展示"仍存在"的问题项**，已解决 / 已失效只在顶部数字与解决率里体现。
解决率 = 已解决 / (已解决 + 仍存在 + 已失效)（失效也计入分母，用户口径）。
"""

from __future__ import annotations

import json

from recheck.recheck import MODULES, MODULE_LABEL

from .encheck import ENCHECK_MODULE
from .linkcheck import LINKCHECK_MODULE
from .ocr import OCR_MODULE

_SRC = {"linkcheck": LINKCHECK_MODULE, "encheck": ENCHECK_MODULE, "ocr": OCR_MODULE}
_ICON = {"linkcheck": "🔗", "encheck": "🌐", "ocr": "🔍"}


def _tab(mk: str) -> dict:
    """用源模块的配置组装一个页签（列/徽标/筛选完全照搬）。"""
    src = _SRC[mk]
    return {
        "key": mk,
        "label": MODULE_LABEL.get(mk, mk),
        "icon": _ICON.get(mk, ""),
        "item_columns": src["item_columns"],
        "badge_map": src.get("badge_map") or {},
        "multi_badge": src.get("multi_badge"),
        "filters": src.get("filters") or [],
    }


TABS = [_tab(mk) for mk in MODULES]


def _recheck_context(db) -> dict:
    """最近一次复核 run 的概览 + 按模块的解决率（供页面比例条，卡片可直链到对应页签）。"""
    row = db._conn.execute(
        "SELECT id, summary_json, started_at FROM runs WHERE module_key='recheck' "
        "AND status='success' ORDER BY id DESC LIMIT 1").fetchone()
    stats = {"total": 0, "resolved": 0, "still": 0, "gone": 0, "ignored": 0, "rate": 0.0}
    per_module: list[dict] = []
    latest_run_id = None
    if not row:
        return {"recheck_stats": stats, "recheck_modules": per_module,
                "recheck_run_id": None}
    run_id, sj, run_started_at = row
    latest_run_id = run_id
    try:
        s = json.loads(sj or "{}")
        stats = {"total": s.get("total", 0), "resolved": s.get("resolved", 0),
                 "still": s.get("still", 0), "gone": s.get("gone", 0),
                 "ignored": s.get("ignored", 0), "rate": s.get("rate", 0.0)}
    except Exception:
        pass
    stats["run_at"] = run_started_at

    agg: dict[str, dict] = {}
    for dj in db._conn.execute("SELECT detail_json FROM items WHERE run_id=?", (run_id,)):
        try:
            d = json.loads(dj[0])
        except Exception:
            continue
        mk = d.get("module") or "?"
        agg.setdefault(mk, {"item_count": 0})["item_count"] += 1
    # 解决率等按 summary 里的按模块计数（口径：问题项）
    sval = _safe(sj)
    for mk in MODULES:
        n = int(sval.get(f"{mk}_total", 0))
        if not n:
            continue
        r = int(sval.get(f"{mk}_resolved", 0))
        st = int(sval.get(f"{mk}_still", 0))
        gn = int(sval.get(f"{mk}_gone", 0))
        ig = int(sval.get(f"{mk}_ignored", 0))
        denom = (n - ig) or 1      # 已忽略不计入解决率分母（用户口径）
        per_module.append({
            "key": mk, "label": MODULE_LABEL.get(mk, mk),
            "total": n, "resolved": r, "still": st, "gone": gn, "ignored": ig,
            "rate": round(r * 100 / denom, 1),
            "p_resolved": round(r * 100 / denom, 1),
            "p_still": round(st * 100 / denom, 1),
            "issue_count": agg.get(mk, {}).get("item_count", 0),
        })
    # 顺序跟 MODULES 走（与首页「每日增量内容检查」一致），不按总量排
    return {"recheck_stats": stats, "recheck_modules": per_module,
            "recheck_run_id": latest_run_id}


def _safe(sj) -> dict:
    try:
        return json.loads(sj or "{}")
    except Exception:
        return {}


RECHECK_MODULE = {
    "key": "recheck",
    "name": "当前全量问题",
    "nav_name": "复核",            # 顶栏菜单用短名
    "icon": "🗓️",
    "debug": False,            # 不再打「调试中」（用户 2026-09）；imgnorm 仍打
    "in_nav": False,             # 不进顶栏菜单（用户 2026-09 要求）；仍保留在首页「结果复核」组里
    "description": "复核历史发现的问题：已解决 / 仍存在 / 已失效（明细只列仍存在的问题）",
    "runs_title": "当前全量问题记录",
    "per_page": 30,
    "tabs": TABS,
    "tab_field": "module",
    "item_columns": TABS[0]["item_columns"],     # 兜底（有 tabs 时以页签为准）
    "summary_fields": [("total", "复核项"), ("resolved", "已解决"), ("still", "仍存在"),
                       ("gone", "已失效"), ("rate", "解决率(%)")],
    # 顶部汇总：前 5 个是「问题点/处」口径（与卡片、各模块页一致），最后「待处理明细」是「文档/篇」口径
    "detail_summary_fields": [("total", "复核项(处)"), ("resolved", "已解决(处)"),
                              ("still", "仍存在(处)"), ("ignored", "已忽略(处)"),
                              ("gone", "已失效(处)"), ("rate", "解决率(%)"),
                              ("items", "待处理明细(篇)")],
    "filters": [],
    "badge_map": {},
    "context_provider": _recheck_context,
}
