"""模块：全量链接健康检查（专项整改板块）。

与「链接健康检查」**同源同口径**，区别只在覆盖范围与执行时间：
  - 增量版 linkcheck     ：每天 1:40，只查 sync 变更的文档
  - 全量版 linkcheck_full：每天 6:00，扫描全部文档

问题类型与结果呈现与增量版**完全一致**：
    断链(404/410) / 误链历史版本 / 锚点失效
后端仍会采集「被拒 / 服务端异常 / 不可达」，但同样不在界面展示。

执行脚本：linkcheck/link_check.py --full --module-key linkcheck_full
（URL 结果共用 linkcheck 的 TTL 缓存：可访问/真死 7 天，异常 1 天）

item.detail 与增量版结构一致：doc_key / lang / catalog / url、
dead_links[] / vintage_links[] / anchor_miss_links[]{text,url,status,kind} 及各自 *_count。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import ignores  # noqa: E402

from catalogs import CATALOG_OPTIONS  # noqa: E402

# 界面上"可见问题"的字段（决定列表是否收录该文档）——与增量版一致
VISIBLE_FIELDS = ("dead_count", "vintage_count", "anchor_miss_count")


def _item_visible(it: dict) -> bool:
    d = it.get("detail") or {}
    return any(d.get(f, 0) > 0 for f in VISIBLE_FIELDS)


def _linkcheck_full_context(db) -> dict:
    """当前全量链接问题概览（供顶部卡片）：取本模块最近一次成功 run。

    注意：与增量版不同，全量 run 本身覆盖全部文档，所以直接取最近一次，
    不需要跨 run 去重。
    """
    stats = {"checked": 0, "dead": 0, "vintage": 0, "anchor_miss": 0}
    rows = db._conn.execute(
        "SELECT i.item_key, i.detail_json FROM items i "
        "WHERE i.run_id=(SELECT MAX(id) FROM runs "
        "                WHERE module_key='linkcheck_full' AND status='success') "
        "ORDER BY i.id ASC").fetchall()
    latest: dict[str, str] = {}
    for item_key, detail_json in rows:
        latest[item_key] = detail_json           # 后写覆盖 → 保留最新一次结果
    try:
        _s = db._conn.execute(
            "SELECT summary_json FROM runs WHERE module_key='linkcheck_full' "
            "AND status='success' ORDER BY id DESC LIMIT 1").fetchone()
        stats["checked"] = (json.loads(_s[0] or "{}").get("checked", 0) if _s else 0) or 0
    except Exception:
        stats["checked"] = len(latest)
    rules = ignores.active_map(db)
    for detail_json in latest.values():
        try:
            d = json.loads(detail_json)
        except Exception:
            continue
        d2, _ign, _rem = ignores.strip("linkcheck_full", d, rules)
        stats["dead"] += d2.get("dead_count", 0)
        stats["vintage"] += d2.get("vintage_count", 0)
        stats["anchor_miss"] += d2.get("anchor_miss_count", 0)
    _ra = db._conn.execute(
        "SELECT started_at FROM runs WHERE module_key='linkcheck_full' "
        "AND status='success' ORDER BY id DESC LIMIT 1").fetchone()
    stats["run_at"] = _ra[0] if _ra else None
    return {"linkcheck_full_stats": stats}


LINKCHECK_FULL_MODULE = {
    "key": "linkcheck_full",
    "name": "全量链接健康检查",
    "icon": "🌐",
    "in_nav": False,          # 专项整改板块约定：不进顶栏菜单
    "description": "全量链接健康检查：每天扫描全部文档的断链 / 误链历史版本 / 锚点失效"
                   "（问题类型与结果呈现与「链接健康检查」一致）",
    "runs_title": "每日全量链接检查记录",
    "summary_fields": [("checked", "检查文档"), ("dead", "断链"),
                       ("vintage", "误链历史版本"), ("anchor_miss", "锚点失效")],
    "detail_summary_fields": [("checked", "检查文档"), ("dead", "断链"),
                              ("vintage", "误链历史版本"),
                              ("anchor_miss", "锚点失效")],
    "item_columns": [
        ("catalog", "分类"),
        ("dead_links", "断链(404/410)", "link_list"),
        ("vintage_links", "历史版本链接", "link_list"),
        ("anchor_miss_links", "锚点失效链接", "link_list"),
        ("url", "源文档"),
    ],
    "filters": [
        {"key": "kit", "label": "Kit", "source": "kit", "default": "all", "options": []},
        {"key": "lang", "label": "语言", "source": "detail",
         "options": [("all", "全部"), ("cn", "中文文档"), ("en", "英文文档")]},
        {"key": "type", "label": "问题", "source": "count",
         "fields": {"dead": "dead_count", "vintage": "vintage_count",
                    "anchor_miss": "anchor_miss_count"},
         "options": [("all", "全部"), ("dead", "断链"),
                     ("vintage", "误链历史版本"), ("anchor_miss", "锚点失效")]},
        {"key": "catalog", "label": "分类", "source": "detail",
         "options": list(CATALOG_OPTIONS)},
        {"key": "sort", "label": "排序", "source": "sort",
         "options": [("id_desc", "默认"), ("dead_count_desc", "断链从高到低"),
                     ("anchor_miss_count_desc", "锚点失效从高到低")]},
    ],
    "multi_badge": [
        ("dead_count", "badge-failed", "断链"),
        ("vintage_count", "badge-deleted", "误链历史版本"),
        ("anchor_miss_count", "badge-info", "锚点失效"),
    ],
    "badge_map": {"dead": ("badge-failed", "链接问题")},
    "context_provider": _linkcheck_full_context,
    "item_visible": _item_visible,
}
