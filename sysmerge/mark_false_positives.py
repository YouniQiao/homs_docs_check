#!/usr/bin/env python3
"""系统合并整改 — 预标「明确误报」的匹配词（整词忽略，target='*'）。

只放**已逐条核对过、确与 AGC/管理中心无关**的词。
混杂词（如「余额」「账号组」）不在此列，留给用户自己判断。

用法：python3 sysmerge/mark_false_positives.py [--undo]
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ignore_store import IgnoreStore  # noqa: E402

FALSE_POSITIVES = {
    "付费服务": "命中的是游戏防沉迷规定中的『游戏付费服务』，与 AGC/管理中心无关",
    "API服务": "命中的是泛指说法（如『REST API服务接口』），非 AGC 的『API服务』菜单",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--undo", action="store_true", help="撤销这些预标")
    args = ap.parse_args()
    s = IgnoreStore()
    if args.undo:
        for term in FALSE_POSITIVES:
            n = s.restore_ignores_for("sysmerge", "*", term, ip="127.0.0.1")
            print(f"  撤销 {term}: {n} 条")
    else:
        for term, why in FALSE_POSITIVES.items():
            ok = s.add_ignore("sysmerge", "*", term,
                              reason=f"系统预标误报：{why}", ip="127.0.0.1",
                              created_by="系统预标")
            print(f"  {'✅ 新增' if ok else '已存在'} 整词忽略：{term}")
    print("\n当前生效的整词忽略：")
    for r in s.all_records(only_active=True):
        if r["target"] == "*":
            print(f"   {r['kind']:12s}  {r['reason'][:60]}")
    s.close()


if __name__ == "__main__":
    main()
