"""模块：图片内容规范检查。

执行脚本：imgnorm/img_norm.py（基于已 OCR 文本，不重跑 OCR）。
每张"有问题"的图一个 item（item_type = "issue"），
detail 字段：image、doc_key、lang、catalog、doc_url、confidence、
             evidence_text（命中摘要）、evidence（{字段: [证据]}）、
             ocr_text（截断）、以及各规则的计数字段 n_<id>。
规则见 imgnorm/rules.py。
"""

from __future__ import annotations

import json

import ignores

from imgnorm.rules import BADGE_CLS, FIELD, LABEL, RULES


def _imgnorm_context(db) -> dict:
    """按图片去重（取最新一次结果）统计各规则命中数（按当前忽略过滤）+ 覆盖图片数。"""
    rows = db._conn.execute(
        "SELECT i.item_key, i.detail_json FROM items i "
        "JOIN runs r ON i.run_id = r.id WHERE r.module_key='imgnorm' "
        "ORDER BY i.id ASC"
    ).fetchall()
    latest: dict[str, str] = {}
    for item_key, detail_json in rows:
        latest[item_key] = detail_json        # 后写覆盖 → 保留最新

    rules = ignores.active_map(db)
    stats = {"issues": 0}
    for r in RULES:
        stats[FIELD[r["id"]]] = 0
    sensitive = 0
    for detail_json in latest.values():
        try:
            d = json.loads(detail_json)
        except Exception:
            continue
        d, _ign, _rem = ignores.strip("imgnorm", d, rules)   # 按忽略过滤
        hit = False
        for r in RULES:
            if d.get(FIELD[r["id"]], 0) > 0:
                stats[FIELD[r["id"]]] += 1
                hit = True
        if hit:
            stats["issues"] += 1
        if any(d.get(FIELD[x], 0) > 0 for x in ("secret", "ip", "phone", "email")):
            sensitive += 1
    stats["n_sensitive"] = sensitive

    # 覆盖图片数 = OCR 已检查的图片数（规范检查基于同一批图）
    try:
        stats["checked"] = db._conn.execute(
            "SELECT COUNT(DISTINCT i.item_key) FROM items i "
            "JOIN runs r ON i.run_id = r.id WHERE r.module_key='ocr'"
        ).fetchone()[0]
    except Exception:
        stats["checked"] = 0

    # 敏感信息合计已在上面按忽略过滤后统计（stats["n_sensitive"]）

    return {"imgnorm_stats": stats}


IMGNORM_MODULE = {
    "key": "imgnorm",
    "name": "图片内容规范检查",
    "nav_name": "图片内容规范检查",  # 顶栏菜单名（用户 2026-09 定）
    "icon": "🖼️",
    "debug": True,   # 调试中：导航/标题/首页卡片显示「调试中」标签
    "description": "基于已识别的图片文字，检查术语规范、敏感信息、占位残留等问题",
    "runs_title": "图片内容规范检查记录",
    "per_page": 30,
    # 记录表列（精简，只留关键维度）
    "summary_fields": [("checked", "检查图片"), ("issues", "问题图片"),
                       ("n_term", "术语/大小写"), ("n_sensitive", "敏感信息"),
                       ("n_watermark", "水印/内部字样"),
                       ("n_placeholder", "占位/测试残留"),
                       ("n_trad", "繁体字"), ("n_reverse_en", "中文图含英文"),
                       ("n_lowq", "低清/模糊")],
    # 详情页概览卡（全维度）
    "detail_summary_fields": [("checked", "检查图片"), ("issues", "问题图片")]
                             + [(FIELD[r["id"]], r["label"]) for r in RULES],
    "item_columns": [("image", "图片"),
                     ("evidence_text", "命中详情"),
                     ("lang", "语言"),
                     ("confidence", "置信度"),
                     ("doc_url", "源文档")],
    "filters": [
        {"key": "type", "label": "问题类型", "source": "count",
         "fields": {r["id"]: FIELD[r["id"]] for r in RULES},
         "options": [("all", "全部")] + [(r["id"], r["label"]) for r in RULES]},
        {"key": "lang", "label": "语言", "source": "detail",
         "options": [("all", "全部"), ("cn", "中文文档"), ("en", "英文文档")]},
        {"key": "dl", "label": "源文档地址包含", "source": "contains",
         "field": "doc_url", "control": "text",
         "placeholder": "如 best-practices 或 image-blur"},
    ],
    # 类型列多标签渲染：(detail 计数字段, badge 样式, 标签文本)
    "multi_badge": [(FIELD[r["id"]], BADGE_CLS[r["id"]], LABEL[r["id"]])
                    for r in RULES],
    "badge_map": {
        "issue": ("badge-modified", "问题"),
    },
    "context_provider": _imgnorm_context,
}
