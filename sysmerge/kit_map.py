#!/usr/bin/env python3
"""从华为目录树推导「文档 → Kit」映射（系统合并整改用）。

原理：目录树里 API参考 的 Kit 分组节点形如 `ability-api`/`arkui-api`，
指南形如 `ability-kit`。文档的 Kit = 其祖先链里最近的 `*-api` / `*-kit` 节点。

输出 sysmerge/kit_map.json：{doc_key: kit_name}
用法：python3 sysmerge/kit_map.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from hw_api import get_catalog_tree  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent / "kit_map.json"
CATALOGS = ["harmonyos-references", "harmonyos-guides"]

# 品牌大小写别名（自动 title-case 会写错的部分）
ALIAS = {"arkui": "ArkUI", "arkts": "ArkTS", "arkweb": "ArkWeb",
         "arkdata": "ArkData", "arkgraphics": "ArkGraphics", "fa": "FA",
         "iot": "IoT", "ui": "UI", "api": "API", "pdf": "PDF", "av": "AV",
         "ipc": "IPC", "nfc": "NFC", "ime": "IME", "drm": "DRM", "rpc": "RPC",
         "cann": "CANN", "cannkit": "CANN", "cannkit-ascend-c": "CANN"}


def _kit_node_name(relate: str) -> str | None:
    """`ability-api` / `ability-kit` / `push-kit-guide` → Ability Kit；非 Kit 节点 → None。"""
    if not relate:
        return None
    m = re.match(r"^(.+?)-(kit-guide|api|kit)$", relate)
    if not m:
        return None
    stem = m.group(1)
    words = []
    for w in stem.split("-"):
        words.append(ALIAS.get(w, w.capitalize() if w.islower() else w))
    return " ".join(words) + " Kit"


def _children(node: dict) -> list:
    for k in ("children", "child", "subCatalog", "catalogTreeList", "subCatalogList"):
        v = node.get(k)
        if isinstance(v, list):
            return v
    return []


def _relate(node: dict) -> str:
    for k in ("relateDocument", "documentName", "relate_document", "file_name"):
        v = node.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def walk(node: dict, catalog: str, lang: str, kit_stack: list, out: dict) -> int:
    """递归：维护祖先里的 Kit 栈，把每个「有 relateDocument 的节点」记进 out。"""
    relate = _relate(node)
    node_kit = _kit_node_name(relate)
    n = 0
    if node_kit:
        kit_stack = kit_stack + [node_kit]
    if relate:
        doc_key = f"{lang}|{catalog}|{relate}"
        if kit_stack:
            out[doc_key] = kit_stack[-1]
        n += 1
    for ch in _children(node):
        if isinstance(ch, dict):
            n += walk(ch, catalog, lang, kit_stack, out)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out: dict[str, str] = {}
    for cat in CATALOGS:
        print(f"📂 拉取目录树 {cat} (cn) ...", flush=True)
        tree = get_catalog_tree(cat, "cn")
        roots = _children(tree) if isinstance(tree, dict) else (tree or [])
        n = 0
        for r in roots:
            if isinstance(r, dict):
                n += walk(r, cat, "cn", [], out)
        kits = {v for v in out.values()}
        print(f"   {cat}: 收录文档 {n} 篇，识别 Kit {len(kits)} 个")
    print(f"\n合计 {len(out)} 篇文档有 Kit；示例：")
    for k, v in list(out.items())[:8]:
        print(f"   {v:22s} {k}")
    if not args.dry_run:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"\n✅ 已写 {OUT}")
    from collections import Counter
    for kit, c in Counter(out.values()).most_common(12):
        print(f"   {kit:24s} {c} 篇")


if __name__ == "__main__":
    main()
