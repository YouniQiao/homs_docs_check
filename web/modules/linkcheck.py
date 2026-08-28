"""模块：链接健康检查。

执行脚本：linkcheck/link_check.py（--full 全量 / 默认增量）。
每篇有问题链接的文档一个 item（正常文档不记录，同 encheck）：
  - int_missing（站内未覆盖）：链接是 developer.huawei.com 站内链接，但指向的
    页面不在本地已同步文档里（新文档 / 未覆盖 catalog / 老版本 V2-V5 等）。
  - ext_dead（外链死链）：非华为外链 HTTP 状态 >= 400。
detail 字段：doc_key/lang/catalog/url、int_links[int_missing_count]、
ext_links[ext_dead_count]、ext_unknown_count。类型列用 multi_badge 独立计数。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

CATALOGS = ["best-practices", "harmonyos-guides", "harmonyos-references",
            "harmonyos-faqs", "harmonyos-releases"]


def _linkcheck_context(db) -> dict:
    """当前链接检查结果统计卡片。"""
    rows = db._conn.execute(
        "SELECT i.detail_json FROM items i JOIN runs r ON i.run_id = r.id "
        "WHERE r.module_key='linkcheck'"
    ).fetchall()
    int_missing = ext_dead = ext_unknown = 0
    cat_map: dict[str, int] = {}
    for (dj,) in rows:
        try:
            d = json.loads(dj)
        except Exception:
            continue
        if d.get("int_missing_count", 0) > 0:
            int_missing += 1
        if d.get("ext_dead_count", 0) > 0:
            ext_dead += 1
        if d.get("ext_unknown_count", 0) > 0:
            ext_unknown += 1
        cat_map[d.get("catalog", "?")] = cat_map.get(d.get("catalog", "?"), 0) + 1
    return {"linkcheck_stats": {"checked": len(rows), "int_missing": int_missing,
                                "ext_dead": ext_dead, "ext_unknown": ext_unknown},
            "catalog_map": cat_map}


LINKCHECK_MODULE = {
    "key": "linkcheck",
    "name": "链接健康检查",
    "icon": "🔗",
    "description": "检测文档链接问题：站内链接指向未覆盖文档 + 外链死链",
    "summary_fields": [("total", "问题文档"), ("int_missing", "站内未覆盖"),
                       ("ext_dead", "外链死链")],
    "detail_summary_fields": [("total", "问题文档"), ("int_missing", "站内未覆盖"),
                              ("ext_dead", "外链死链"),
                              ("ext_unknown", "外链未知")],
    "item_columns": [
        ("catalog", "分类"),
        ("int_links", "站内未覆盖", "link_list"),
        ("ext_links", "外链死链", "link_list"),
        ("url", "源文档"),
    ],
    "filters": [
        {"key": "type", "label": "问题", "source": "count",
         "fields": {"int_missing": "int_missing_count", "ext_dead": "ext_dead_count"},
         "options": [("all", "全部"), ("int_missing", "站内未覆盖"),
                     ("ext_dead", "外链死链")]},
        {"key": "catalog", "label": "分类", "source": "detail",
         "options": [("all", "全部")] + [(c, c) for c in CATALOGS]},
        {"key": "sort", "label": "排序", "source": "sort",
         "options": [("id_desc", "默认"), ("int_desc", "未覆盖数从高到低"),
                     ("ext_desc", "死链数从高到低")]},
    ],
    "multi_badge": [
        ("int_missing_count", "badge-modified", "站内未覆盖"),
        ("ext_dead_count", "badge-failed", "外链死链"),
    ],
    "context_provider": _linkcheck_context,
}