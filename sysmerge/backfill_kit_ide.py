#!/usr/bin/env python3
"""一次性回填 docs.kit / docs.ide 两列（P0 第二步）。

数据来源（由 sysmerge/kit_map.py 从华为目录树推导，只读）：
  sysmerge/kit_map.v2.json   {doc_key: kit}        18270 条（cn 9165 / en 9105）
  sysmerge/ide_map.json      {doc_key: ide_group}   1030 条（cn 549 / en 481）
  doc_key = "{lang}|{catalog}|{relateDocument}"

只覆盖 harmonyos-guides + harmonyos-references 两个 catalog（映射文件本身即如此，
faqs / releases / best-practices 不纳入，这两列保持 NULL）。

写入方式：UPDATE docs SET kit=? WHERE doc_key=?（ide 同理），仅更新 docs 表中
真实存在的 doc_key；不动任何其他表。

用法：
  python3 sysmerge/backfill_kit_ide.py --dry-run   # 只统计不写库
  python3 sysmerge/backfill_kit_ide.py             # 执行回填
  python3 sysmerge/backfill_kit_ide.py --db /path/to/index.db
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from db import IndexDB  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
KIT_MAP_PATH = HERE / "kit_map.v2.json"
IDE_MAP_PATH = HERE / "ide_map.json"
DEFAULT_DB = BASE_DIR / "index.db"

# 覆盖率只统计这两个 catalog（映射的覆盖范围）
SCOPE_CATALOGS = ("harmonyos-guides", "harmonyos-references")


def load_map(path: pathlib.Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"❌ 映射格式异常（应为 dict）: {path}")
    return data


def report_coverage(conn, label: str):
    """打印范围内各 lang/catalog 的 kit/ide 覆盖率。"""
    print(f"\n【{label}】docs 表覆盖情况（catalog ∈ {SCOPE_CATALOGS}）")
    print(f"  {'lang':4s} {'catalog':22s} {'docs':>6s} {'有kit':>6s} {'kit覆盖':>8s} "
          f"{'有ide':>6s} {'ide覆盖':>8s}")
    for lang in ("cn", "en"):
        t_all = k_all = i_all = 0
        for cat in SCOPE_CATALOGS:
            row = conn.execute(
                "SELECT COUNT(*),"
                " SUM(CASE WHEN kit IS NOT NULL AND kit<>'' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN ide IS NOT NULL AND ide<>'' THEN 1 ELSE 0 END)"
                " FROM docs WHERE lang=? AND catalog=?", (lang, cat)).fetchone()
            total, with_kit, with_ide = row[0] or 0, row[1] or 0, row[2] or 0
            t_all += total; k_all += with_kit; i_all += with_ide
            print(f"  {lang:4s} {cat:22s} {total:6d} {with_kit:6d} "
                  f"{with_kit / total * 100 if total else 0:7.1f}% "
                  f"{with_ide:6d} {with_ide / total * 100 if total else 0:7.1f}%")
        print(f"  {lang:4s} {'- 合计':22s} {t_all:6d} {k_all:6d} "
              f"{k_all / t_all * 100 if t_all else 0:7.1f}% "
              f"{i_all:6d} {i_all / t_all * 100 if t_all else 0:7.1f}%")

    row = conn.execute(
        "SELECT COUNT(*),"
        " SUM(CASE WHEN kit IS NOT NULL AND kit<>'' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN ide IS NOT NULL AND ide<>'' THEN 1 ELSE 0 END)"
        " FROM docs").fetchone()
    print(f"\n  docs 全表: {row[0]} 行；有 kit {row[1] or 0} 行；有 ide {row[2] or 0} 行")


def main():
    ap = argparse.ArgumentParser(description="回填 docs.kit / docs.ide")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="index.db 路径")
    args = ap.parse_args()

    kit_map = load_map(KIT_MAP_PATH)
    ide_map = load_map(IDE_MAP_PATH)
    print(f"📄 映射文件: kit {len(kit_map)} 条 / ide {len(ide_map)} 条")
    for lang in ("cn", "en"):
        print(f"   {lang}: kit {sum(1 for k in kit_map if k.startswith(lang + '|'))} 条，"
              f"ide {sum(1 for k in ide_map if k.startswith(lang + '|'))} 条")

    # IndexDB 会执行建表 SCHEMA + ALTER TABLE 迁移，确保 kit/ide 列与索引已就绪
    db = IndexDB(args.db)
    conn = db._conn
    total_rows = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    print(f"\n🗄  docs 表: {args.db}（{total_rows} 行）")

    doc_keys = {r[0] for r in conn.execute("SELECT doc_key FROM docs")}
    kit_rows = [(kit, dk) for dk, kit in kit_map.items() if dk in doc_keys]
    ide_rows = [(ide, dk) for dk, ide in ide_map.items() if dk in doc_keys]
    kit_miss = sorted(set(kit_map) - doc_keys)
    ide_miss = sorted(set(ide_map) - doc_keys)

    print(f"   映射命中 docs 的 doc_key: kit {len(kit_rows)} / {len(kit_map)}，"
          f"ide {len(ide_rows)} / {len(ide_map)}")
    if kit_miss:
        print(f"   （kit 映射中 {len(kit_miss)} 条不在 docs，例："
              f"{', '.join(kit_miss[:3])}）")
    if ide_miss:
        print(f"   （ide 映射中 {len(ide_miss)} 条不在 docs，例："
              f"{', '.join(ide_miss[:3])}）")

    if args.dry_run:
        print("\n(dry-run，未写库)")
        report_coverage(conn, "dry-run 现状")
        db.close()
        return

    cur = conn.executemany("UPDATE docs SET kit=? WHERE doc_key=?", kit_rows)
    kit_updated = cur.rowcount
    cur = conn.executemany("UPDATE docs SET ide=? WHERE doc_key=?", ide_rows)
    ide_updated = cur.rowcount
    db.commit()

    print(f"\n✅ 回填完成: kit 更新 {kit_updated} 行，ide 更新 {ide_updated} 行")

    after_rows = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    print(f"   docs 总行数: 回填前 {total_rows} → 回填后 {after_rows}"
          f"（{'一致' if after_rows == total_rows else '❌ 不一致'}）")

    report_coverage(conn, "回填后")
    db.close()


if __name__ == "__main__":
    main()
