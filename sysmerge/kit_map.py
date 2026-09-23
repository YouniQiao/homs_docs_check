#!/usr/bin/env python3
"""从华为目录树推导「文档 → Kit」与「文档 → IDE 分组」映射（系统合并整改用）。

v2 变更（修 bug，2026-09）：
  1) Kit 节点判定改为「以目录树 nodeName 为准 + 必须是分组节点(isLeaf=False)
     + relateDocument 形如 -api/-kit/-guide」三重条件。
     旧版用 `^(.+?)-(kit-guide|api|kit)$` 猜 relateDocument，会把 ide-hvigor-api、
     system-security-api、errorcode-xxx-api 这类**文档/栏目节点自身**误判成 Kit
     （289 条 key 自我指向），本版不再单独使用该正则。
  2) Kit 名直接取自 nodeName（去掉中文括号后缀），因此不会再出现
     "Mechanic Kit Kit" / "Accessory Kit Kit" 这类重复后缀噪声。
  3) 保留并完善 ALIAS：拆成 WORD_ALIAS（词级品牌大小写修正）与
     CANON（整名规范化，对齐看板 kit_mapping.yaml），另加 KIT_STOPWORDS 过滤
     "FAQs About Xxx Kit" / "About This Kit" 这类栏目节点。
  4) 同一套逻辑跑 cn / en 两遍（en 用 get_catalog_tree(catalog, "en")），
     en 侧 Kit 名经 cn 侧 relateDocument→Kit 表对齐，保证中英同名同 Kit。
  5) 额外产出 IDE 分组映射：relate_document 以 `ide-` 前缀为唯一可靠信号，
     分组名取该节点所在目录树的**一级(L0)节点名**（中文），en 侧复用 cn 组名。

输出：
  sysmerge/kit_map.v2.json   {doc_key: kit}        doc_key = "{lang}|{catalog}|{relateDocument}"
  sysmerge/ide_map.json      {doc_key: ide_group}
  （--legacy 时额外覆盖 sysmerge/kit_map.json，默认不动它）

用法：
  python3 sysmerge/kit_map.py --dry-run      # 只打印统计，不写文件
  python3 sysmerge/kit_map.py                # 写 kit_map.v2.json + ide_map.json
  python3 sysmerge/kit_map.py --legacy       # 额外覆盖 kit_map.json（兼容旧调用方）
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sqlite3
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from hw_api import get_catalog_tree  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
OUT_KIT = HERE / "kit_map.v2.json"
OUT_IDE = HERE / "ide_map.json"
OUT_LEGACY = HERE / "kit_map.json"
DB = BASE_DIR / "index.db"
DASHBOARD_YAML = pathlib.Path("/opt/projects/oh_docs_dashboard/kit_mapping.yaml")

CATALOGS = ["harmonyos-references", "harmonyos-guides"]
LANGS = ["cn", "en"]

# ---------------------------------------------------------------- Kit 名规范化
# 词级品牌大小写修正（旧版 ALIAS 的保留部分；对 nodeName 派生的名字做一次兜底修正）
WORD_ALIAS = {
    "arkui": "ArkUI", "arkts": "ArkTS", "arkweb": "ArkWeb",
    "arkdata": "ArkData", "arkgraphics": "ArkGraphics", "fa": "FA",
    "iot": "IoT", "ui": "UI", "api": "API", "pdf": "PDF", "av": "AV",
    "ipc": "IPC", "nfc": "NFC", "ime": "IME", "drm": "DRM", "rpc": "RPC",
    "cann": "CANN", "mdm": "MDM", "ffrt": "FFRT", "iap": "IAP", "dpi": "DPI",
    "gpu": "GPU", "ai": "AI", "ar": "AR", "vr": "VR", "npu": "NPU", "aod": "AOD",
}

# 整名规范化：目录树名 → 看板/文档通用名（对齐 oh_docs_dashboard/kit_mapping.yaml）
CANON = {
    "Function Flow Runtime Kit": "FFRT Kit",   # 看板名 FFRT Kit（api_dir apis-ffrt-kit）
}

# 非 "Xxx Kit" 结尾、但确实是 Kit 品牌组的节点名（ArkUI 系）
KIT_BRANDS = {"ArkData", "ArkTS", "ArkUI", "ArkWeb", "ArkGraphics 2D", "ArkGraphics 3D"}

# 栏目节点黑名单：nodeName 命中即判为非 Kit（防 "FAQs About Xxx Kit"/"About This Kit"）
KIT_STOPWORDS = ("faq", "about", "overview", "introduction", "release note",
                 "changelog", "appendix", "best practice", "tutorial")

# 严格 Kit 节点判定：nodeName（去中文括号后缀）为 "Xxx Kit" 或已知品牌组
_KIT_RE = re.compile(
    r"^(?P<base>.+? Kit|ArkData|ArkTS|ArkUI|ArkWeb|ArkGraphics 2D|ArkGraphics 3D)"
    r"([（(].*[）)])?$"
)
# Kit 分组节点的 relateDocument 形如 ability-kit / arkui-api / aod-navigation-guide
_KIT_RELATE_RE = re.compile(r"-(api|kit|guide)$")

IDE_PREFIX = "ide-"


def _norm_name(base: str) -> str:
    """对 Kit 名做词级品牌大小写兜底修正 + 整名规范化。"""
    base = re.sub(r"\s+", " ", base).strip()
    words = [WORD_ALIAS.get(w.lower(), w) if w.islower() else w for w in base.split(" ")]
    out = " ".join(words)
    return CANON.get(out, out)


def kit_node_name(node: dict) -> str | None:
    """严格判定「Kit 分组节点」，返回规范化 Kit 名；不是 Kit 节点 → None。

    三重条件：非叶子分组节点 + nodeName 为 "Xxx Kit"/已知品牌组
              + relateDocument 形如 -api/-kit/-guide（品牌组豁免）。
    """
    if not isinstance(node, dict) or node.get("isLeaf"):
        return None
    name = (node.get("nodeName") or "").strip()
    if not name:
        return None
    m = _KIT_RE.match(name)
    if not m:
        return None
    if not _children(node):
        return None
    base = m.group("base").strip()
    low = base.lower()
    if any(s in low for s in KIT_STOPWORDS):
        return None
    if base not in KIT_BRANDS and not _KIT_RELATE_RE.search(_relate(node)):
        return None
    return _norm_name(base)


# ------------------------------------------------------------------ 树遍历工具
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


def _roots(tree) -> list:
    if isinstance(tree, dict):
        return [r for r in (tree.get("catalogTreeList") or []) if isinstance(r, dict)]
    return [r for r in (tree or []) if isinstance(r, dict)]


def walk(node, catalog, lang, kit_stack, l0, out_kit, out_ide, kit_relate,
         cn_ide_group, stats):
    """递归：Kit 栈取最近祖先 Kit 节点；IDE 分组取所在 L0 节点名。"""
    relate = _relate(node)
    kit = kit_node_name(node)
    if kit:
        kit_stack = kit_stack + [kit]
        if relate:
            kit_relate.setdefault(relate, kit)
    if relate:
        doc_key = f"{lang}|{catalog}|{relate}"
        stats["nodes"] += 1
        if kit_stack:
            out_kit[doc_key] = kit_stack[-1]
        else:
            stats["no_kit_nodes"] += 1
            stats[f"nokit::{l0}"] += 1
        if relate.startswith(IDE_PREFIX) and l0:
            grp = cn_ide_group.get(relate) or l0
            out_ide[doc_key] = grp
    for ch in _children(node):
        if isinstance(ch, dict):
            walk(ch, catalog, lang, kit_stack, l0, out_kit, out_ide, kit_relate,
                 cn_ide_group, stats)


def harvest(tree, catalog, lang, cn_ide_group=None):
    """遍历一棵目录树。返回 (out_kit, out_ide, kit_relate, ide_relate, stats)。"""
    cn_ide_group = cn_ide_group or {}
    out_kit, out_ide, kit_relate, ide_relate = {}, {}, {}, {}
    stats = collections.Counter()
    for r in _roots(tree):
        l0 = (r.get("nodeName") or "").strip()
        walk(r, catalog, lang, [], l0, out_kit, out_ide, kit_relate,
             cn_ide_group, stats)
    for k in list(out_ide):
        ide_relate[k.split("|", 2)[2]] = out_ide[k]
    return out_kit, out_ide, kit_relate, ide_relate, stats


def fetch(lang: str):
    trees = {}
    for cat in CATALOGS:
        print(f"📂 拉取目录树 {cat} ({lang}) ...", flush=True)
        trees[cat] = get_catalog_tree(cat, lang)
    return trees


def load_dashboard_kits() -> set:
    """读看板 kit_mapping.yaml 的 Kit 名（只读，用于对比）。"""
    if not DASHBOARD_YAML.exists():
        return set()
    names = set()
    for line in DASHBOARD_YAML.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*-?\s*name:\s*(.+?)\s*$", line)
        if m:
            names.add(m.group(1))
    return names


def docs_coverage(kit_map: dict, ide_map: dict):
    """与 index.db 的 docs 表交叉验证（只读）。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute("SELECT doc_key, lang, catalog FROM docs").fetchall()
    con.close()
    tot = collections.Counter()
    hit_kit = collections.Counter()
    hit_ide = collections.Counter()
    nokit_by_cat = collections.Counter()
    nokit_samples = collections.defaultdict(list)
    for doc_key, lang, catalog in rows:
        tot[(lang, catalog)] += 1
        if doc_key in kit_map:
            hit_kit[(lang, catalog)] += 1
        else:
            nokit_by_cat[(lang, catalog)] += 1
            if len(nokit_samples[(lang, catalog)]) < 6:
                nokit_samples[(lang, catalog)].append(doc_key.split("|", 2)[2])
        if doc_key in ide_map:
            hit_ide[(lang, catalog)] += 1
    return tot, hit_kit, hit_ide, nokit_by_cat, nokit_samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    ap.add_argument("--legacy", action="store_true",
                    help="额外覆盖 sysmerge/kit_map.json（旧调用方兼容）")
    args = ap.parse_args()

    # ---- pass 1: cn（建立 cn ide 组名 + cn Kit 名规范表）
    trees_cn = fetch("cn")
    cn_kit, cn_ide, cn_kit_relate, cn_ide_relate = {}, {}, {}, {}
    nokit_l0 = collections.Counter()
    for cat in CATALOGS:
        k, i, kr, ir, st = harvest(trees_cn[cat], cat, "cn")
        cn_kit.update(k); cn_ide.update(i); cn_kit_relate.update(kr); cn_ide_relate.update(ir)
        for key, v in st.items():
            if isinstance(key, str) and key.startswith("nokit::"):
                nokit_l0[("cn", key[7:])] += v
        print(f"   cn {cat}: 节点 {st['nodes']} 篇，定 Kit {len(k)} 篇，IDE 分组 {len(i)} 篇，"
              f"Kit 名 {len(set(k.values()))} 个")

    # ---- pass 2: en（复用 cn 的 relate→Kit / relate→IDE组 对齐）
    trees_en = fetch("en")
    kit_map, ide_map = dict(cn_kit), dict(cn_ide)
    en_kit_name_diff = collections.Counter()
    for cat in CATALOGS:
        k, i, kr, ir, st = harvest(trees_en[cat], cat, "en", cn_ide_group=cn_ide_relate)
        fixed = {}
        for dk, name in k.items():
            rel = dk.split("|", 2)[2]
            canonical = cn_kit_relate.get(rel, name)
            if canonical != name:
                en_kit_name_diff[(name, canonical)] += 1
            fixed[dk] = canonical
        kit_map.update(fixed); ide_map.update(i)
        for key, v in st.items():
            if isinstance(key, str) and key.startswith("nokit::"):
                nokit_l0[("en", key[7:])] += v
        print(f"   en {cat}: 节点 {st['nodes']} 篇，定 Kit {len(k)} 篇，IDE 分组 {len(i)} 篇，"
              f"Kit 名 {len(set(fixed.values()))} 个")

    # ---------------------------------------------------------------- 报告
    kits = collections.Counter(kit_map.values())
    print(f"\n{'=' * 72}\n【映射规模】")
    print(f"  kit_map.v2 : {len(kit_map)} 条 doc_key，去重 Kit 名 {len(kits)} 个")
    print(f"  ide_map    : {len(ide_map)} 条 doc_key，分组 {len(set(ide_map.values()))} 个")
    for lang in LANGS:
        print(f"    {lang}: kit {sum(1 for dk in kit_map if dk.startswith(lang + '|'))} 条，"
              f"ide {sum(1 for dk in ide_map if dk.startswith(lang + '|'))} 条")

    print("\n【Kit 名 Top 20（按 doc_key 数）】")
    for name, c in kits.most_common(20):
        print(f"  {c:5d}  {name}")

    print("\n【IDE 分组清单】")
    for name, c in collections.Counter(ide_map.values()).most_common():
        print(f"  {c:5d}  {name}")

    dash = load_dashboard_kits()
    if dash:
        missing = sorted(set(kits) - dash)
        print(f"\n【与看板 kit_mapping.yaml 对比】看板 {len(dash)} 个；"
              f"新映射覆盖看板 {len(dash & set(kits))} 个；"
              f"新映射多出 {len(missing)} 个（目录树存在、看板未收）")
        if missing:
            print("  多出：" + "、".join(missing[:80]) + (" ..." if len(missing) > 80 else ""))

    if en_kit_name_diff:
        print(f"\n【en→cn Kit 名对齐】{len(en_kit_name_diff)} 组差异（en名 → cn名）")
        for (a, b), c in en_kit_name_diff.most_common(15):
            print(f"  {a!r} → {b!r}  ×{c}")
    else:
        print("\n【en→cn Kit 名对齐】0 组差异（中英 Kit 名完全一致）")

    if DB.exists():
        tot, hk, hi, nokit, samples = docs_coverage(kit_map, ide_map)
        print("\n【与 docs 表交叉验证（覆盖率）】")
        print(f"  {'lang/catalog':30s} {'docs':>6s} {'有Kit':>6s} {'覆盖率':>8s} {'有IDE组':>7s}")
        for lang in LANGS:
            t_all = sum(v for (l, c), v in tot.items() if l == lang)
            h_all = sum(v for (l, c), v in hk.items() if l == lang)
            i_all = sum(v for (l, c), v in hi.items() if l == lang)
            print(f"  {'- ' + lang + ' 合计':30s} {t_all:6d} {h_all:6d} "
                  f"{h_all / t_all * 100:7.1f}% {i_all:7d}")
            for (l, c), t in sorted(tot.items(), key=lambda x: (x[0][0], -x[1])):
                if l != lang:
                    continue
                h = hk.get((l, c), 0)
                print(f"    {c:28s} {t:6d} {h:6d} {h / t * 100:7.1f}% {hi.get((l, c), 0):7d}")
        print("\n【仍无 Kit 的文档（按 catalog，示例为 relateDocument）】")
        for (l, c), v in sorted(nokit.items(), key=lambda x: -x[1]):
            print(f"  {l}/{c:26s} {v:6d}  例：{', '.join(samples[(l, c)][:3])}")
        print("\n【guides 仍无 Kit 的文档：按一级(L0)章节分布】")
        for (l, l0), v in sorted(nokit_l0.items(), key=lambda x: -x[1]):
            if v:
                print(f"  {l}  {l0:34s} {v:5d}")

    if not args.dry_run:
        OUT_KIT.write_text(json.dumps(kit_map, ensure_ascii=False, indent=0, sort_keys=True),
                           encoding="utf-8")
        OUT_IDE.write_text(json.dumps(ide_map, ensure_ascii=False, indent=0, sort_keys=True),
                           encoding="utf-8")
        print(f"\n✅ 已写 {OUT_KIT}（{len(kit_map)} 条）")
        print(f"✅ 已写 {OUT_IDE}（{len(ide_map)} 条）")
        if args.legacy:
            OUT_LEGACY.write_text(json.dumps(kit_map, ensure_ascii=False, indent=0, sort_keys=True),
                                  encoding="utf-8")
            print(f"✅ 已覆盖旧路径 {OUT_LEGACY}")
    else:
        print("\n(dry-run，未写文件)")


if __name__ == "__main__":
    main()
