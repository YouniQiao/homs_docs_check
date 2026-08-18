"""模块：文档同步（华为开发者文档增量同步）。"""

from __future__ import annotations

from pathlib import Path

# 项目根目录（web/modules/sync.py 上三级）
BASE_DIR = Path(__file__).resolve().parent.parent.parent


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


def _sync_context(db) -> dict:
    return {"catalog_stats": build_catalog_stats(db)}


SYNC_MODULE = {
    "key": "sync",
    "name": "文档同步",
    "icon": "📚",
    "description": "华为开发者文档增量同步（displayUpdateTime 判据 + 并发下载）",
    "summary_fields": [("added", "新增"), ("modified", "修改"), ("deleted", "删除")],
    "item_columns": [("title", "标题"), ("lang", "语言"), ("catalog", "分类"),
                     ("url", "链接")],
    "context_provider": _sync_context,
}
