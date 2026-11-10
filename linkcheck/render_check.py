#!/usr/bin/python3
"""伪 200 渲染复核：headless shell 渲染，识别「HTTP 200 但页面实际 404」。

用法: render_check.py <urls.json> <out.jsonl> [workers]
输出: 每行 {"url":..., "verdict":"ok|dead|unknown", "sec":...}

判据（2026-11 用已知死链 + 随机正常页校准过：死链 5/5 命中、正常页 0 误报）：
  * dead   : 前端路由切到 /404/ —— <title> 含 404 且**没有** h1.doc-title
  * ok     : 有 h1.doc-title（真文档页；旧 ID 会被 getUrlMapping 重映射，仍算存活）
  * unknown: DOM 太小 / 超时 / 结构不符（判不了，可重试）

并发（踩过的坑，勿改）：
  * 必须用 chrome-headless-shell + 每个 worker 独立 --user-data-dir
  * 完整版 chromium 加 --user-data-dir 会挂死（历史坑），所以这里优先 shell
  * 不要加 --blink-settings=imagesEnabled=false（404 路由页会挂 40s 无输出）
"""
from __future__ import annotations

import concurrent.futures
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

SHELL_PATTERNS = [
    os.environ.get("CHROME_SHELL_BIN", ""),
    os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/"
                       "chrome-headless-shell-linux64/chrome-headless-shell"),
    "/usr/bin/chrome-headless-shell",
]
CHROME_PATTERNS = [
    os.environ.get("CHROME_BIN", ""),
    os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
    os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    "/usr/bin/chromium", "/usr/bin/google-chrome",
]

_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.I)
_H1_RE = re.compile(r'<h1[^>]*class="[^"]*\bdoc-title\b[^"]*"', re.I)


def _find(patterns: list[str]) -> str | None:
    for pat in patterns:
        if not pat:
            continue
        hits = [p for p in glob.glob(pat) if os.path.isfile(p) and os.access(p, os.X_OK)]
        if hits:
            return sorted(hits)[-1]
    return None


def browser_bin() -> str:
    """优先 chrome-headless-shell（并发安全），否则退回完整 chromium。"""
    exe = _find(SHELL_PATTERNS) or _find(CHROME_PATTERNS)
    if not exe:
        raise SystemExit("找不到 chrome-headless-shell / chrome")
    return exe


def judge(dom: str) -> str:
    if len(dom) < 500:
        return "unknown"
    if _H1_RE.search(dom):          # 有 doc-title → 真文档页
        return "ok"
    m = _TITLE_RE.search(dom)
    title = m.group(1) if m else ""
    return "dead" if "404" in title else "unknown"


def render(url: str, exe: str, timeout: int | None = None) -> tuple[str, float]:
    timeout = timeout or int(os.environ.get("RC_TIMEOUT", "30"))
    prof = tempfile.mkdtemp(prefix="pseudo404-")
    headless = [] if "headless-shell" in os.path.basename(exe) else ["--headless"]
    t0 = time.time()
    try:
        p = subprocess.run(
            [exe, *headless, "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--virtual-time-budget=8000",
             "--timeout=25000",            # 重页面强制 25s 落盘，避免无限等网络空闲
             f"--user-data-dir={prof}", "--dump-dom", url],
            capture_output=True, timeout=timeout, text=True, errors="ignore")
        dom = p.stdout or ""
    except subprocess.TimeoutExpired:
        return "unknown", round(time.time() - t0, 1)
    except Exception:
        return "unknown", round(time.time() - t0, 1)
    finally:
        shutil.rmtree(prof, ignore_errors=True)
    return judge(dom), round(time.time() - t0, 1)


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("用法: render_check.py <urls.json> <out.jsonl> [workers]")
    urls_file, out_file = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else int(os.environ.get("RC_WORKERS", "8"))
    urls = json.load(open(urls_file, encoding="utf-8"))
    exe = browser_bin()
    print(f"引擎: {exe}\n条数: {len(urls)}  并发: {workers}", flush=True)
    t0 = time.time()
    done = 0
    with open(out_file, "w", encoding="utf-8") as fh, \
            concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(render, u, exe): u for u in urls}
        for fut in concurrent.futures.as_completed(futs):
            u = futs[fut]
            try:
                verdict, sec = fut.result()
            except Exception:
                verdict, sec = "unknown", 0.0
            fh.write(json.dumps({"url": u, "verdict": verdict, "sec": sec},
                                ensure_ascii=False) + "\n")
            fh.flush()
            done += 1
            if done % 20 == 0:
                el = time.time() - t0
                print(f"进度 {done}/{len(urls)}  {verdict}  "
                      f"({el:.0f}s, {el/done:.1f}s/条)", flush=True)
    print(f"完成 {len(urls)}/{len(urls)}，耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
