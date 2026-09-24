"""模块：文档同步（鸿蒙开发者文档每日增量同步）。"""

from __future__ import annotations

import json
from pathlib import Path

# 项目根目录（web/modules/sync.py 上三级）
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 分类英文 key → 中文名（未知 key 原样显示）
from catalogs import CATALOG_LABELS, CATALOG_OPTIONS  # noqa: E402
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
    # 最新一条同步 run（供「每日增量数据」区块展示新增/修改/删除）
    latest = db._conn.execute(
        "SELECT started_at, summary_json FROM runs WHERE module_key='sync' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    sync_latest, sync_latest_at = {}, None
    if latest:
        sync_latest_at = latest[0]
        try:
            sync_latest = json.loads(latest[1] or "{}")
        except Exception:
            sync_latest = {}
    return {"catalog_stats": build_catalog_stats(db),
            "catalog_overview": build_catalog_overview(db),
            "sync_latest": sync_latest, "sync_latest_at": sync_latest_at}


SYNC_FILTERS = [
    {"key": "lang", "label": "语言", "source": "detail",
     "options": [("all", "全部"), ("cn", "中文文档"), ("en", "英文文档")]},
    {"key": "catalog", "label": "分类", "source": "detail",
     "options": list(CATALOG_OPTIONS)},
    {"key": "type", "label": "变更", "source": "item_type",
     "options": [("all", "全部"), ("added", "新增"), ("modified", "修改"),
                 ("deleted", "删除")]},
    {"key": "q", "label": "标题包含", "source": "contains", "field": "title",
     "control": "text", "placeholder": "标题关键字"},
]


SYNC_MODULE = {
    "key": "sync",
    "name": "每日文档增量记录",
    "nav_name": "文档同步",        # 顶栏菜单名保持短名（用户 2026-09 定；首页卡片/页标题用全名）
    "in_nav": False,              # 用户 2026-09：从顶栏菜单去掉（首页「数据同步」组仍有卡片）
    "icon": "📚",
    "description": "鸿蒙开发者文档每日增量同步（根据页面displayUpdateTime判断是否变更）",
    "runs_title": "同步任务记录",
    "summary_fields": [("added", "新增"), ("modified", "修改"), ("deleted", "删除")],
    "filters": SYNC_FILTERS,
    "item_columns": [("title", "标题"), ("lang", "语言"), ("catalog", "分类"),
                     ("url", "链接")],
    "context_provider": _sync_context,
}
