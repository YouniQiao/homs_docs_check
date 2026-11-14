#!/usr/bin/python3
"""清理 docscheck 的孤儿图片：data/<lang>/<cat>/images 下未被任何同级 md 引用的文件。

用法：
  prune_orphan_images.py --dry-run     # 只统计 + 生成清单
  prune_orphan_images.py --execute     # 真删

安全措施：
  - 只删「同名 md 目录下没有任何 ![](images/xxx) 引用」的文件
  - 引用写法的全库校验见交付说明（无 <img>、无 ./images/、无 /images/ 变体）
  - 生成 JSONL 清单（路径+大小），可审计
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

IMG_RE = re.compile(r'!\[[^\]]*\]\(images/([^)\s"]+)')
ROOT = "/opt/projects/harmonyos_docs"
DATA = os.path.join(ROOT, "data")


def collect(min_age_days: int = 0) -> tuple[list[tuple[str, int]], int, int]:
    cutoff = time.time() - min_age_days * 86400
    orphans: list[tuple[str, int]] = []
    ref_n = ref_sz = 0
    for lang in ("cn", "en"):
        lbase = os.path.join(DATA, lang)
        if not os.path.isdir(lbase):
            continue
        for cat in sorted(os.listdir(lbase)):
            cdir = os.path.join(lbase, cat)
            idir = os.path.join(cdir, "images")
            if not os.path.isdir(idir):
                continue
            referenced: set[str] = set()
            for md in os.listdir(cdir):
                if not md.endswith(".md"):
                    continue
                try:
                    referenced.update(
                        IMG_RE.findall(open(os.path.join(cdir, md), encoding="utf-8",
                                            errors="ignore").read()))
                except OSError:
                    pass
            with os.scandir(idir) as it:
                for e in it:
                    if not e.is_file():
                        continue
                    st = e.stat()
                    s = st.st_size
                    if e.name in referenced:
                        ref_n += 1
                        ref_sz += s
                    elif st.st_mtime <= cutoff:
                        orphans.append((e.path, s))
    return orphans, ref_n, ref_sz


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--execute", action="store_true")
    ap.add_argument("--min-age", type=int, default=0, metavar="DAYS",
                    help="只删 mtime 早于 N 天的孤儿图（默认 0 = 不限制）")
    ap.add_argument("--quiet", action="store_true", help="只输出最后一行摘要")
    args = ap.parse_args()

    t0 = time.time()
    if not args.quiet:
        print("扫描引用关系…", flush=True)
    orphans, ref_n, ref_sz = collect(args.min_age)
    GB = 1024 ** 3
    tot = sum(s for _, s in orphans)
    if not args.quiet:
        print(f"  在用图 {ref_n:,} 张 {ref_sz/GB:.2f} GB", flush=True)
    print(f"  孤儿图 {len(orphans):,} 张 {tot/GB:.2f} GB"
          + (f"（>={args.min_age} 天）" if args.min_age else ""), flush=True)

    manifest = os.path.join(ROOT, "logs",
                            f"orphan_images_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
    with open(manifest, "w", encoding="utf-8") as fh:
        for p, s in orphans:
            fh.write(json.dumps({"path": os.path.relpath(p, ROOT), "size": s},
                                ensure_ascii=False) + "\n")
    print(f"  清单已写入 {manifest}", flush=True)

    if args.dry_run:
        print("[dry-run] 未删除任何文件", flush=True)
        return 0

    done = freed = 0
    errs = 0
    for p, s in orphans:
        try:
            os.remove(p)
            done += 1
            freed += s
        except FileNotFoundError:
            pass
        except OSError as e:
            errs += 1
            if errs <= 3:
                print(f"  ! 删除失败 {p}: {e}", flush=True)
        if done and done % 20000 == 0:
            print(f"    进度 {done:,}/{len(orphans):,}  已释放 {freed/GB:.1f} GB", flush=True)
    print(f"✅ 删除完成：{done:,} 个文件，释放 {freed/GB:.2f} GB，失败 {errs} 个，"
          f"用时 {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
