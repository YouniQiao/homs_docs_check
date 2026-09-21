"""模块：文档同步（鸿蒙开发者文档每日增量同步）。"""

from __future__ import annotations

from pathlib import Path

# 项目根目录（web/modules/sync.py 上三级）
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 分类英文 key → 中文名（未知 key 原样显示）
CATALOG_LABELS = {
    "harmonyos-guides": "开发指南",
    "harmonyos-references": "API 参考",
    "harmonyos-faqs": "常见问题",
    "harmonyos-releases": "版本说明",
    "best-practices": "最佳实践",
}
LANG_LABELS = {"cn": "中文", "en": "英文"}


def build_catalog_stats(db) -> list[dict]:
    """按语言+分类统计文档数（来自 db）和图片数（来自 data/ 目录文件系统）。"""
    stats = db.stats()
    data_dir = BASE_DIR / "data"
    result = []
    for lang_dir in sorted(data_dir.iterdir()):
        if not lang_dir.is_dir() or lang_dir.name.startswith("."):
            continue
        lang = lang_dir.name
        for cat_dir in sorted(lang_dir.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                continue
            catalog = cat_dir.name
            docs = stats.get("by_lang_catalog", {}).get((lang, catalog), 0)
            img_dir = cat_dir / "images"
            imgs = sum(1 for _ in img_dir.iterdir()) if img_dir.exists() else 0
            result.append({"lang": lang, "catalog": catalog,
                           "docs": docs, "imgs": imgs})
    return result


def build_catalog_overview(db) -> dict:
    """分组总览：总量 + 每语言（含各分类的文档数/图片数/相对占比），供卡片式展示。"""
    stats = db.stats()
    data_dir = BASE_DIR / "data"
    langs: list[dict] = []
    tot_docs = tot_imgs = 0
    for lang_dir in sorted(data_dir.iterdir()):
        if not lang_dir.is_dir() or lang_dir.name.startswith("."):
            continue
        lang = lang_dir.name
        cats: list[dict] = []
        l_docs = l_imgs = 0
        for cat_dir in sorted(lang_dir.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                continue
            catalog = cat_dir.name
            docs = stats.get("by_lang_catalog", {}).get((lang, catalog), 0)
            img_dir = cat_dir / "images"
            imgs = sum(1 for _ in img_dir.iterdir()) if img_dir.exists() else 0
            cats.append({"key": catalog,
                         "label": CATALOG_LABELS.get(catalog, catalog),
                         "docs": docs, "imgs": imgs})
            l_docs += docs
            l_imgs += imgs
        # 条宽 = 该分类文档数相对本语言最大值的百分比
        mx = max((c["docs"] for c in cats), default=0) or 1
        for c in cats:
            c["pct"] = round(c["docs"] * 100 / mx, 1)
        cats.sort(key=lambda c: c["docs"], reverse=True)
        langs.append({"lang": lang, "label": LANG_LABELS.get(lang, lang.upper()),
                      "docs": l_docs, "imgs": l_imgs, "catalogs": cats})
        tot_docs += l_docs
        tot_imgs += l_imgs
    return {"totals": {"docs": tot_docs, "imgs": tot_imgs}, "langs": langs}


def _sync_context(db) -> dict:
    return {"catalog_stats": build_catalog_stats(db),
            "catalog_overview": build_catalog_overview(db)}


SYNC_FILTERS = [
    {"key": "lang", "label": "语言", "source": "detail",
     "options": [("all", "全部"), ("cn", "中文文档"), ("en", "英文文档")]},
    {"key": "catalog", "label": "分类", "source": "detail",
     "options": [("all", "全部")] + [(k, v) for k, v in CATALOG_LABELS.items()]},
    {"key": "type", "label": "变更", "source": "item_type",
     "options": [("all", "全部"), ("added", "新增"), ("modified", "修改"),
                 ("deleted", "删除")]},
    {"key": "q", "label": "标题包含", "source": "contains", "field": "title",
     "control": "text", "placeholder": "标题关键字"},
]


SYNC_MODULE = {
    "key": "sync",
    "name": "文档同步",
    "nav_name": "文档同步",        # 顶栏菜单名（用户 2026-09 定；首页/标题仍用全名）
    "icon": "📚",
    "description": "鸿蒙开发者文档每日增量同步（根据页面displayUpdateTime判断是否变更）",
    "runs_title": "同步任务记录",
    "summary_fields": [("added", "新增"), ("modified", "修改"), ("deleted", "删除")],
    "filters": SYNC_FILTERS,
    "item_columns": [("title", "标题"), ("lang", "语言"), ("catalog", "分类"),
                     ("url", "链接")],
    "context_provider": _sync_context,
}
