"""模块：系统合并整改（专项整改板块）。

背景：AGC 系统与管理中心已合并为新的「HarmonyOS 开发者中心」，
文档中描述的老系统名称/功能、以及指向老文档的链接需要被识别出来统一整改。

执行脚本：sysmerge/scan.py（扫中文文档，一篇文档 × 一个命中项 = 一条 item）。
item.detail：doc_url(原文链接)、doc_title、content_type(归属内容)、kit、
             matched(匹配词)、matched_kind(关键词/链接)、count(命中次数)、
             snippet(上下文片段)、local_path、doc_key。

忽略：**独立库** sysmerge/ignore.db（用户要求，不放 index.db），
      通过 ignores.register_backend 接入；粒度 = 文档 doc_key × 匹配词
      （target="*" 表示该匹配词整词忽略）。
"""

from __future__ import annotations

import json
import pathlib

import ignores

from sysmerge.ignore_store import IgnoreStore

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent   # /opt/projects/harmonyos_docs
KW_FILE = ROOT / "sysmerge" / "keywords.json"
KIT_FILE = ROOT / "sysmerge" / "kit_map.json"

# 注册独立忽略后端（忽略记录写 sysmerge/ignore.db）
ignores.register_backend("sysmerge", lambda: IgnoreStore())

CAT2TYPE = {"harmonyos-guides": "指南", "harmonyos-references": "API参考",
            "best-practices": "最佳实践", "harmonyos-releases": "版本说明",
            "harmonyos-faqs": "FAQ"}


def load_keywords() -> dict:
    try:
        return json.loads(KW_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"terms": [], "links": []}


def load_kits() -> list[str]:
    try:
        return sorted(set(json.loads(KIT_FILE.read_text(encoding="utf-8")).values()))
    except Exception:
        return []


def _sysmerge_context(db) -> dict:
    """概览卡：取最近一次 run 的条目，按当前忽略状态重算各维度计数。"""
    rules = ignores.active_map(db)
    rows = db._conn.execute(
        "SELECT id, summary_json FROM runs WHERE module_key='sysmerge' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    stats = {"checked": 0, "hit_docs": 0, "issues": 0, "ignored": 0, "no_kit": 0}
    if rows:
        rid = rows[0]
        try:
            s = json.loads(rows[1] or "{}")
            stats["checked"] = s.get("checked", 0)
            stats["hit_docs"] = s.get("hit_docs", 0)
            stats["no_kit"] = s.get("no_kit", 0)
        except Exception:
            pass
        by_type: dict[str, int] = {}
        n = n_ign = 0
        for (dj,) in db._conn.execute(
                "SELECT detail_json FROM items WHERE run_id=?", (rid,)):
            try:
                d = json.loads(dj or "{}")
            except Exception:
                continue
            d2, ign, _rem = ignores.strip("sysmerge", d, rules)
            if ign:
                n_ign += 1
                continue
            n += 1
            t = d2.get("content_type", "")
            by_type[t] = by_type.get(t, 0) + 1
        stats["issues"] = n
        stats["ignored"] = n_ign
        for t, c in by_type.items():
            stats[f"type_{t}"] = c
    return {"sysmerge_stats": stats,
            "kw_count": len(load_keywords().get("terms", [])) + len(load_keywords().get("links", []))}


SYSMERGE_MODULE = {
    "key": "sysmerge",
    "name": "系统合并整改",
    "icon": "🧩",
    "in_nav": False,          # 不进顶栏菜单（专项整改板块约定）
    "description": "识别中文文档中 AGC / 管理中心等老系统的名称、功能与老文档链接，"
                   "用于合并到「HarmonyOS 开发者中心」后的统一整改",
    "runs_title": "系统合并整改记录",
    "per_page": 50,
    "summary_fields": [("checked", "扫描文档"), ("hit_docs", "命中文档"),
                       ("issues", "命中条目"), ("no_kit", "未识别Kit")],
    "detail_summary_fields": [("checked", "扫描文档"), ("hit_docs", "命中文档"),
                              ("issues", "待整改"), ("ignored", "已忽略"),
                              ("no_kit", "未识别Kit")],
    "item_columns": [("matched", "匹配词"),
                     ("matched_kind", "方式"),
                     ("content_type", "归属内容"),
                     ("kit", "Kit"),
                     ("doc_title", "文档"),
                     ("count", "次数"),
                     ("snippet", "上下文"),
                     ("doc_url", "原文链接", "link")],
    "filters": [
        {"key": "content_type", "label": "归属内容", "source": "detail",
         "options": [("all", "全部")] + [(t, t) for t in CAT2TYPE.values()]},
        {"key": "kit", "label": "Kit", "source": "detail",
         "options": [("all", "全部")] + [(k, k) for k in load_kits()]},
        {"key": "matched", "label": "匹配词", "source": "detail",
         "options": [("all", "全部")] + [(t, t) for t in load_keywords().get("terms", [])]},
        {"key": "matched_kind", "label": "方式", "source": "detail",
         "options": [("all", "全部"), ("关键词", "关键词"), ("链接", "链接")]},
        {"key": "dl", "label": "文档地址包含", "source": "contains",
         "field": "doc_url", "control": "text", "placeholder": "如 harmonyos-guides"},
    ],
    "badge_map": {"issue": ("badge-modified", "待整改")},
    "context_provider": _sysmerge_context,
    "list_actions": [("⚙️ 关键词配置", "keywords", "编辑待识别关键词与老文档链接")],
}
