#!/usr/bin/env python3
"""系统合并整改 — 扫描器。

在中文文档里检索「老系统（AGC/管理中心）相关」的关键词与老文档链接，
命中即建条目（一篇文档 × 一个命中项 = 一条 item）。

数据维度：原文链接 / 匹配词(关键词或链接) / 归属内容(指南·API参考·最佳实践·版本说明·FAQ)
          / Kit(仅指南与API参考，来自目录树) / 命中次数 / 上下文片段 / 本地文件

用法：
  python3 sysmerge/scan.py --dry-run     # 只统计不写库
  python3 sysmerge/scan.py               # 写库（新建一个 run）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from db import IndexDB  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
KW_FILE = HERE / "keywords.json"
KIT_FILE = HERE / "kit_map.json"

# 栏目 → 中文归属内容
CAT2TYPE = {"harmonyos-guides": "指南", "harmonyos-references": "API参考",
            "best-practices": "最佳实践", "harmonyos-releases": "版本说明",
            "harmonyos-faqs": "FAQ"}
# 需要 Kit 的归属内容（用户要求：仅指南与 API参考）
NEED_KIT = {"指南", "API参考"}


def load_kw() -> tuple[list[str], list[str]]:
    d = json.loads(KW_FILE.read_text(encoding="utf-8"))
    return d.get("terms", []), d.get("links", [])


def snippet(txt: str, idx: int, width: int = 28) -> str:
    a = max(0, idx - width)
    b = min(len(txt), idx + width)
    s = txt[a:b].replace("\n", " ").replace("|", "／").strip()
    return ("…" if a else "") + s + ("…" if b < len(txt) else "")


def main():
    ap = argparse.ArgumentParser(description="系统合并整改扫描")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    terms, links = load_kw()
    kitmap = json.loads(KIT_FILE.read_text(encoding="utf-8")) if KIT_FILE.exists() else {}

    db = IndexDB(str(BASE_DIR / "index.db"))
    # 以 docs 表为准遍历（doc_key 用 relate_document，才能与目录树/其它模块对齐；
    # 文件名可能被 `@` 转义，如 _a_b_r___camera_data）
    docs = list(db._conn.execute(
        "SELECT doc_key,catalog,title,url,local_path FROM docs WHERE lang='cn'"))

    run_id = None if args.dry_run else db.start_run("sysmerge")
    t0 = time.time()
    n_doc = n_item = 0
    by_type: dict[str, int] = {}
    by_term: dict[str, int] = {}
    no_kit = 0

    for doc_key, cat, title, url, local_path in docs:
        ctype = CAT2TYPE.get(cat)
        if not ctype:
            continue
        md = BASE_DIR / (local_path or "")
        if not md.exists():
            continue
        try:
            txt = md.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        hits = []          # [(匹配词, 是否链接, 次数, 首个片段)]
        for t in terms:
            c = txt.count(t)
            if c:
                hits.append((t, False, c, snippet(txt, txt.find(t))))
        for u in links:
            c = txt.count(u)
            if c:
                hits.append((u, True, c, snippet(txt, txt.find(u))))
        if not hits:
            continue
        n_doc += 1
        by_type[ctype] = by_type.get(ctype, 0) + 1
        kit = kitmap.get(doc_key, "") if ctype in NEED_KIT else ""
        if ctype in NEED_KIT and not kit:
            no_kit += 1
        for term, is_link, cnt, snip in hits:
            n_item += 1
            by_term[term] = by_term.get(term, 0) + 1
            if run_id is not None:
                detail = {
                    "doc_url": url or "",
                    "doc_title": title or "",
                    "content_type": ctype,
                    "kit": kit,
                    "matched": term,
                    "matched_kind": "链接" if is_link else "关键词",
                    "count": cnt,
                    "snippet": snip,
                    "local_path": local_path or "",
                    "doc_key": doc_key,
                }
                db.add_item(run_id, f"{doc_key}|{term}", "issue", detail)

    elapsed = time.time() - t0
    summary = {"checked": len(docs), "hit_docs": n_doc, "issues": n_item,
               "no_kit": no_kit, "elapsed_sec": round(elapsed)}
    summary.update({f"type_{k}": v for k, v in by_type.items()})
    if run_id is not None:
        db.finish_run(run_id, summary)
    db.close()

    print(f"{'[dry-run] ' if args.dry_run else ''}扫描文档 {len(docs):,} 篇 | 命中 {n_doc:,} 篇 | "
          f"条目 {n_item:,} 条 | 耗时 {elapsed:.1f}s")
    print("按归属内容：", by_type)
    print(f"指南/API参考 未识别到 Kit 的文档：{no_kit} 篇")
    print("\n按命中项 Top：")
    for k, v in sorted(by_term.items(), key=lambda x: -x[1])[:15]:
        print(f"   {v:5,} 篇   {k[:64]}")
    if run_id is not None:
        print(f"\n✅ 写入 run #{run_id}")


if __name__ == "__main__":
    main()
