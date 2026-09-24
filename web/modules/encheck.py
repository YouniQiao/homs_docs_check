"""模块：英文文档检查（中文字符 + 中文链接）。

执行脚本：encheck/en_check.py（--full 全量 / 默认增量）。
每篇文档一个 item：item_type = problem / clean / error；
类型列按 multi_badge 四项独立计数渲染多标签（含汉字 / 含标点 / 链接URL含中文 / 含中文链接），
不存在"两者都有"这种组合类型。
detail: doc_key, title, doc_url, catalog,
        hanzi, hanzi_count, punct, punct_count,
        url_cn_chars, url_cn_char_count, url_cn_links, cn_links, cn_link_count
"""

from __future__ import annotations

import json

from catalogs import CATALOG_LABELS, CATALOG_OPTIONS  # noqa: E402

import ignores


def _encheck_context(db) -> dict:
    """按文档去重（取最新一次结果）统计各项问题数，供顶部概览卡片展示。"""
    # 顶部 = 最新一日的检查（最近一次成功 run，增量口径；用户 2026-09 定）
    rows = db._conn.execute(
        "SELECT i.item_key, i.item_type, i.detail_json FROM items i "
        "WHERE i.run_id=(SELECT MAX(id) FROM runs WHERE module_key='encheck' AND status='success') "
        "ORDER BY i.id ASC"
    ).fetchall()
    latest: dict[str, tuple] = {}
    for item_key, item_type, detail_json in rows:
        latest[item_key] = (item_type, detail_json)  # 后写覆盖 → 保留最新

    # "检查文档"取本次（最新一日）run 的检查数（正常文档不写 item，从 run 摘要取）
    try:
        _s = db._conn.execute(
            "SELECT summary_json FROM runs WHERE module_key='encheck' AND status='success' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        checked_total = (json.loads(_s[0] or "{}").get("total") if _s else 0) or 0
    except Exception:
        checked_total = len(latest)

    stats = {"checked": checked_total, "hanzi": 0, "punct": 0, "url_cn": 0,
             "cn_link": 0, "clean": 0, "errors": 0}
    rules = ignores.active_map(db)
    for item_type, detail_json in latest.values():
        if item_type == "error":
            stats["errors"] += 1
            continue
        try:
            d = json.loads(detail_json)
        except Exception:
            continue
        d, _ign, _rem = ignores.strip("encheck", d, rules)   # 按忽略过滤后统计
        hit = False
        if d.get("hanzi_count", 0) > 0:
            stats["hanzi"] += 1
            hit = True
        if d.get("punct_count", 0) > 0:
            stats["punct"] += 1
            hit = True
        if d.get("url_cn_char_count", 0) > 0:
            stats["url_cn"] += 1
            hit = True
        if d.get("cn_link_count", 0) > 0:
            stats["cn_link"] += 1
            hit = True
        if not hit:
            stats["clean"] += 1
    _ra = db._conn.execute(
        "SELECT started_at FROM runs WHERE module_key='encheck' AND status='success' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    stats["run_at"] = _ra[0] if _ra else None
    return {"encheck_stats": stats}


ENCHECK_MODULE = {
    "key": "encheck",
    "name": "英文文档检查",
    "nav_name": "英文文档检查",    # 顶栏菜单名（用户 2026-09 定）
    "icon": "🌐",
    "description": "检查英文文档中的中文字符与中文跳转链接",
    "runs_title": "每日增量内容英文文档检查记录",
    "summary_fields": [("total", "检查文档"), ("hanzi", "含汉字"),
                       ("url_cn", "链接URL含中文"), ("cn_link", "含中文链接")],
    "detail_summary_fields": [("total", "检查文档"), ("hanzi", "含汉字"),
                              ("punct", "含标点"),
                              ("url_cn", "链接URL含中文"),
                              ("cn_link", "含中文链接"),
                              ("errors", "错误")],
    "item_columns": [("catalog", "分类"),
                     ("hanzi", "中文汉字"),
                     ("punct", "中文标点"),
                     ("url_cn_links", "链接URL中文", "link_list"),
                     ("cn_links", "中文链接", "link_list"),
                     ("url", "源文档")],
    "filters": [
        {"key": "kit", "label": "Kit", "source": "kit", "default": "all", "options": []},
        {"key": "type", "label": "问题", "source": "count",
         "fields": {"hanzi": "hanzi_count", "punct": "punct_count",
                    "url_cn": "url_cn_char_count", "cn_link": "cn_link_count"},
         "options": [("all", "全部"), ("hanzi", "含汉字"),
                     ("punct", "含标点"), ("url_cn", "链接URL含中文"),
                     ("cn_link", "含中文链接")]},
        {"key": "catalog", "label": "分类", "source": "detail",
         "options": list(CATALOG_OPTIONS)},
        {"key": "dl", "label": "源文档地址包含", "source": "contains",
         "field": "doc_url", "control": "text",
         "placeholder": "如 best-practices 或 avplayer"},
    ],
    # 类型列多标签渲染：(detail 计数字段, badge 样式, 标签文本)；全为 0 显示"正常"
    "multi_badge": [
        ("hanzi_count", "badge-failed", "含汉字"),
        ("punct_count", "badge-modified", "含标点"),
        ("url_cn_char_count", "badge-info", "链接URL含中文"),
        ("cn_link_count", "badge-added", "含中文链接"),
    ],
    "badge_map": {
        "problem": ("badge-modified", "含中文"),
        "clean": ("badge-added", "正常"),
        "error": ("badge-failed", "读取失败"),
    },
    "context_provider": _encheck_context,
}
