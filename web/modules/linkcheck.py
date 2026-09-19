"""模块：链接健康检查（展示：检查文档 / 真死链 / 误链历史版本 / 锚点失效）。

执行脚本：linkcheck/link_check.py（--full 全量 / 默认增量；URL 结果带 TTL 缓存）。
后端仍会采集"被拒 / 服务端异常 / 不可达"，但**不在界面展示**（本模块只暴露需要的维度）。

"检查文档"= 本次检查覆盖的文档数（summary.checked），与其他模块口径一致；
列表只列"有可见问题（真死链/历史版本/锚点失效）"的文档。
detail: doc_key/lang/catalog/url、*_links[]{text,url,status,kind}、*_count
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

# 界面上"可见问题"的字段（决定列表是否收录该文档）
VISIBLE_FIELDS = ("dead_count", "vintage_count", "anchor_miss_count")


def _item_visible(it: dict) -> bool:
    d = it.get("detail") or {}
    return any(d.get(f, 0) > 0 for f in VISIBLE_FIELDS)


def _linkcheck_context(db) -> dict:
    """最近一次检查的概览（供顶部卡片）。"""
    row = db._conn.execute(
        "SELECT summary_json FROM runs WHERE module_key='linkcheck' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    stats = {"checked": 0, "dead": 0, "vintage": 0, "anchor_miss": 0}
    if row and row[0]:
        try:
            s = json.loads(row[0])
            stats = {"checked": s.get("checked", 0), "dead": s.get("dead", 0),
                     "vintage": s.get("vintage", 0),
                     "anchor_miss": s.get("anchor_miss", 0)}
        except Exception:
            pass
    return {"linkcheck_stats": stats}


LINKCHECK_MODULE = {
    "key": "linkcheck",
    "name": "链接健康检查",
    "icon": "🔗",
    "description": "链接健康检查：真死链 / 误链历史版本 / 锚点失效（真实 HTTP + 缓存 TTL）",
    "runs_title": "链接检查记录",
    "summary_fields": [("checked", "检查文档"), ("dead", "真死链"),
                       ("vintage", "误链历史版本"), ("anchor_miss", "锚点失效")],
    "detail_summary_fields": [("checked", "检查文档"), ("dead", "真死链"),
                              ("vintage", "误链历史版本"),
                              ("anchor_miss", "锚点失效")],
    "item_columns": [
        ("catalog", "分类"),
        ("dead_links", "真死链(404/410)", "link_list"),
        ("vintage_links", "历史版本链接", "link_list"),
        ("anchor_miss_links", "锚点失效链接", "link_list"),
        ("url", "源文档"),
    ],
    "filters": [
        {"key": "type", "label": "问题", "source": "count",
         "fields": {"dead": "dead_count", "vintage": "vintage_count",
                    "anchor_miss": "anchor_miss_count"},
         "options": [("all", "全部"), ("dead", "真死链"),
                     ("vintage", "误链历史版本"), ("anchor_miss", "锚点失效")]},
        {"key": "catalog", "label": "分类", "source": "detail",
         "options": [("all", "全部")] + [(c, c) for c in CATALOGS]},
        {"key": "sort", "label": "排序", "source": "sort",
         "options": [("id_desc", "默认"), ("dead_count_desc", "真死链从高到低"),
                     ("anchor_miss_count_desc", "锚点失效从高到低")]},
    ],
    "multi_badge": [
        ("dead_count", "badge-failed", "真死链"),
        ("vintage_count", "badge-deleted", "误链历史版本"),
        ("anchor_miss_count", "badge-info", "锚点失效"),
    ],
    "badge_map": {"dead": ("badge-failed", "链接问题")},
    "context_provider": _linkcheck_context,
    "item_visible": _item_visible,
}
