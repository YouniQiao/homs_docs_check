"""模块：图片 OCR 检查。

执行脚本：ocr/ocr_check.py（--full 全量 / 默认增量 + 多进程）。
每张图一个 item：item_type = "has_cn" / "no_cn" / "error"，
detail_json 字段：image（data/ 相对路径）、doc_key、doc_url、ocr_text、
lines、has_cn、confidence。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import ignores  # noqa: E402


def _ocr_context(db) -> dict:
    """OCR 统计卡片 + 最新英文图含中文列表。"""
    rows = db._conn.execute(
        "SELECT i.item_type, i.detail_json, i.item_key FROM items i "
        "JOIN runs r ON i.run_id = r.id WHERE r.module_key='ocr' "
        "ORDER BY r.id ASC, i.id ASC"     # 升序 → 同图后写覆盖，保留最新一次结果
    ).fetchall()
    checked: set[str] = set()
    has_cn = en_has_cn = errors = 0
    rules = ignores.active_map(db)
    for item_type, detail_json, item_key in rows:
        if item_key in checked:
            continue
        checked.add(item_key)
        if item_type == "error":
            errors += 1
        elif item_type == "has_cn":
            try:
                d = json.loads(detail_json)
            except Exception:
                continue
            d, _ign, _rem = ignores.strip("ocr", d, rules, item_type)   # 按忽略过滤
            if d.get("has_cn"):
                has_cn += 1
                if d.get("lang") == "en":
                    en_has_cn += 1

    # 最新英文含中文图（按 run 倒序 + 去重，取 8 张）——暂不在首页展示，保留计算供后续启用
    en_cn_images: list[dict] = []
    seen: set[str] = set()
    for row in db._conn.execute(
        "SELECT i.item_key, i.detail_json FROM items i "
        "JOIN runs r ON i.run_id = r.id "
        "WHERE r.module_key='ocr' AND i.item_type='has_cn' "
        "ORDER BY r.id DESC, i.id DESC"
    ).fetchall():
        item_key, detail_json = row
        if item_key in seen:
            continue
        seen.add(item_key)
        try:
            d = json.loads(detail_json)
        except Exception:
            continue
        if d.get("lang") == "en":
            en_cn_images.append(d)
            if len(en_cn_images) >= 8:
                break

    return {"ocr_stats": {"checked": len(checked), "has_cn": has_cn,
                          "en_has_cn": en_has_cn, "errors": errors},
            "en_cn_images": en_cn_images}


OCR_MODULE = {
    "key": "ocr",
    "name": "图片 OCR 检查",
    "icon": "🔍",
    "description": "检测文档图片中的中文（PaddleOCR），全量 + 每日增量",
    "runs_title": "图片OCR检查记录",
    "per_page": 30,
    "summary_fields": [("total", "检查总数"), ("has_cn", "含中文"),
                       ("en_has_cn", "英文图含中文"), ("errors", "错误")],
    "detail_summary_fields": [("total", "检查总数"), ("has_cn", "含中文"),
                              ("en_has_cn", "英文图含中文"),
                              ("errors", "错误")],
    "item_columns": [("image", "图片"), ("lang", "语言"), ("ocr_text", "识别文字"),
                     ("confidence", "置信度"), ("doc_url", "来源文档")],
    "filters": [
        {"key": "lang", "label": "语言", "source": "detail", "default": "en",
         "options": [("all", "全部"), ("cn", "中文文档"), ("en", "英文文档")]},
        {"key": "type", "label": "检出", "source": "item_type", "default": "has_cn",
         "options": [("all", "全部"), ("has_cn", "含中文"), ("no_cn", "无中文"),
                     ("error", "识别失败")]},
        {"key": "sort", "label": "排序", "source": "sort", "default": "id_desc",
         "options": [("id_desc", "默认"), ("conf_desc", "置信度从高到低"),
                     ("conf_asc", "置信度从低到高")]},
    ],
    "badge_map": {
        "has_cn": ("badge-modified", "含中文"),
        "no_cn": ("badge-added", "无中文"),
        "error": ("badge-failed", "识别失败"),
    },
    "context_provider": _ocr_context,
}
