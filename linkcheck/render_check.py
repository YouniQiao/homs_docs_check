#!/usr/bin/python3
"""伪 200 渲染复核：headless chrome 渲染，识别「HTTP 200 但页面实际 404」。

用法: render_check.py <urls.json> <out.jsonl>
输出: 每行 {"url":..., "verdict":"ok|dead|timeout|error", "sec":...}

注意（踩过的坑，勿改）：
  * 必须串行（workers=1）：不给 --user-data-dir 时共用临时 profile，并发会锁死。
  * 不能加 --user-data-dir：该 chromium 构建下指定 profile 目录会挂死（各种 flag 都救不回来）。
  * 不能加 --blink-settings=imagesEnabled=false：会让 404 路由页挂死 40s 无输出。
"""
import glob
import json
import os
import subprocess
import sys
import time

CHROME_PATTERNS = [
    os.environ.get("CHROME_BIN", ""),
    os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
    os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    "/usr/bin/chromium", "/usr/bin/google-chrome",
]
DEAD_MARKS = ("您访问的页面不存在", "🐴 404")


def chrome_bin() -> str:
    for pat in CHROME_PATTERNS:
        if not pat:
            continue
        hits = [p for p in glob.glob(pat) if os.path.isfile(p) and os.access(p, os.X_OK)]
        if hits:
            return sorted(hits)[-1]
    raise SystemExit("找不到 chrome")


def render(url: str, exe: str, timeout: int | None = None) -> tuple[str, float]:
    timeout = timeout or int(os.environ.get("RC_TIMEOUT", "30"))
    t0 = time.time()
    try:
        p = subprocess.run(
            [exe, "--headless", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--virtual-time-budget=6000",
             "--timeout=12000",          # 强制在 12s 落盘，避免重页面无限等网络空闲
             "--dump-dom", url],
            capture_output=True, timeout=timeout, text=True, errors="ignore")
        dom = p.stdout or ""
    except subprocess.TimeoutExpired:
        return "timeout", round(time.time() - t0, 1)
    except Exception:
        return "error", round(time.time() - t0, 1)
    dt = round(time.time() - t0, 1)
    if len(dom) < 500:
        return "error", dt
    return ("dead" if any(m in dom for m in DEAD_MARKS) else "ok"), dt


def main() -> None:
    urls_file, out_file = sys.argv[1], sys.argv[2]
    urls = json.load(open(urls_file, encoding="utf-8"))
    exe = chrome_bin()
    with open(out_file, "w", encoding="utf-8") as fh:
        for i, u in enumerate(urls, 1):
            verdict, sec = render(u, exe)
            fh.write(json.dumps({"url": u, "verdict": verdict, "sec": sec},
                                ensure_ascii=False) + "\n")
            fh.flush()
            if i % 10 == 0:
                print(f"进度 {i}/{len(urls)}  {verdict} {sec}s", flush=True)
    print(f"完成 {len(urls)}/{len(urls)}", flush=True)


if __name__ == "__main__":
    main()
