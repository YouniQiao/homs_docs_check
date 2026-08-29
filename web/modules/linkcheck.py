"""模块：链接健康检查（外部真死链检查）。

执行脚本：linkcheck/link_check.py（--full 全量 / 默认增量；外部 URL 结果缓存）。
只真实 HTTP 检查**外部链接**（华为站内因反爬不批量检查，且大多指向已同步文档=有效）。
每篇有问题外部链接的文档一个 item：
  - dead：HTTP 状态码 >= 400（404/403/410/5xx 等）
  - unreachable：连接超时 / 解析失败 / ERR
detail：doc_key/lang/catalog/url、dead_links[]{text,url,status}、
dead_count、unreachable_count。
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
    """当前链接检查结果统计（外部死链/不可达问题文档数）。"""
    rows = db._conn.execute(
        "SELECT i.detail_json FROM items i JOIN runs r ON i.run_id = r.id "
        "WHERE r.module_key='linkcheck'"
    ).fetchall()
    dead = unk = 0
    cat_map: dict[str, int] = {}
    for (dj,) in rows:
        try:
            d = json.loads(dj)
        except Exception:
            continue
        if d.get("dead_count", 0) > 0:
            dead += 1
        if d.get("unreachable_count", 0) > 0:
            unk += 1
        cat_map[d.get("catalog", "?")] = cat_map.get(d.get("catalog", "?"), 0) + 1
    return {"linkcheck_stats": {"checked": len(rows), "ext_dead": dead,
                                "unreachable": unk},
            "catalog_map": cat_map}


LINKCHECK_MODULE = {
    "key": "linkcheck",
    "name": "链接健康检查",
    "icon": "🔗",
    "description": "外部链接真死链检查：真实 HTTP 访问，检测无法打开的页面",
    "summary_fields": [("total", "问题文档"), ("ext_dead", "外部死链"),
                       ("unreachable", "不可达"), ("vintage", "误链历史版本"),
                       ("anchor_miss", "锚点失效")],
    "detail_summary_fields": [("total", "问题文档"), ("ext_dead", "外部死链"),
                              ("unreachable", "不可达"),
                              ("vintage", "误链历史版本"),
                              ("anchor_miss", "锚点失效")],
    "item_columns": [
        ("catalog", "分类"),
        ("dead_links", "外部死链/不可达链接", "link_list"),
        ("vintage_links", "历史版本链接", "link_list"),
        ("anchor_miss_links", "锚点失效链接", "link_list"),
        ("url", "源文档"),
    ],
    "filters": [
        {"key": "type", "label": "问题", "source": "count",
         "fields": {"ext_dead": "dead_count", "unreachable": "unreachable_count",
                     "vintage": "vintage_count", "anchor_miss": "anchor_miss_count"},
         "options": [("all", "全部"), ("ext_dead", "外部死链"),
                     ("unreachable", "不可达"), ("vintage", "误链历史版本"),
                     ("anchor_miss", "锚点失效")]},
        {"key": "catalog", "label": "分类", "source": "detail",
         "options": [("all", "全部")] + [(c, c) for c in CATALOGS]},
        {"key": "sort", "label": "排序", "source": "sort",
         "options": [("id_desc", "默认"), ("ext_desc", "死链数从高到低")]},
    ],
    "multi_badge": [
        ("dead_count", "badge-failed", "外部死链"),
        ("unreachable_count", "badge-modified", "不可达"),
        ("vintage_count", "badge-deleted", "误链历史版本"),
        ("anchor_miss_count", "badge-modified", "锚点失效"),
    ],
    "badge_map": {"dead": ("badge-failed", "链接问题")},
    "context_provider": _linkcheck_context,
}