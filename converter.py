"""HTML → Markdown 转换 + 图片本地化。

流程：
  1. 从原始 HTML 提取所有 <img src>
  2. markitdown 将 HTML 转为 Markdown
  3. 下载每张图片到 catalog 的 images/ 目录（按 URL md5 命名，全局去重）
  4. 把 Markdown 中的 CDN URL 替换为本地相对路径 images/xxx.ext
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import html as html_mod
import io
import re
from pathlib import Path

import requests
from markitdown import MarkItDown

IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
MD_IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

# Content-Type -> 扩展名
EXT_MAP = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/webp": "webp",
    "image/bmp": "bmp",
}

SITE_BASE = "https://developer.huawei.com"

_md = MarkItDown()


def _resolve_url(src: str) -> str:
    """相对路径补全为完整 URL，并反转义 HTML 实体（&amp; -> &）。"""
    src = html_mod.unescape(src.strip())
    if src.startswith("http://") or src.startswith("https://"):
        return src
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return SITE_BASE + src
    return SITE_BASE + "/" + src


def _ext_from_headers(content_type: str, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in EXT_MAP:
        return EXT_MAP[ct]
    # 从 URL 猜
    path = url.split("?")[0]
    for ext in ("png", "jpg", "jpeg", "gif", "svg", "webp", "bmp"):
        if path.lower().endswith("." + ext):
            return "jpeg" if ext == "jpeg" else ext
    return "png"  # 默认


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def convert_and_localize(html: str, images_dir: Path,
                         session: requests.Session | None = None) -> tuple[str, int]:
    """转换 HTML 为 Markdown，并本地化图片。

    返回 (markdown_text, 成功下载的图片数)。
    """
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://developer.huawei.com/consumer/en/doc/",
        })

    # 1. 提取原始图片 URL（unescape HTML 实体，去重，保序）
    raw_srcs = IMG_RE.findall(html)
    seen = set()
    srcs = []
    for s in raw_srcs:
        s = html_mod.unescape(s.strip())
        if s and s not in seen:
            seen.add(s)
            srcs.append(s)

    # 2. markitdown 转换
    result = _md.convert_stream(io.BytesIO(html.encode("utf-8")), file_extension=".html")
    text = result.text_content

    # 3. 串行下载图片（复用 session 连接池；跨文档去重）
    images_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    local_map: dict[str, str] = {}  # 原始 URL -> 本地相对路径

    for src in srcs:
        full_url = _resolve_url(src)
        # 用去掉签名参数（? 之后）的 base URL 做文件名，保证同一张图签名变化时也能去重
        base_url = full_url.split("?")[0]
        ext_guess = _ext_from_headers("", full_url)
        fname = f"{_md5(base_url)}.{ext_guess}"
        if (images_dir / fname).exists():
            # 已下载过，直接复用
            local_map[src] = f"images/{fname}"
            continue
        try:
            r = session.get(full_url, timeout=30)
            if r.status_code == 200 and r.content:
                ext = _ext_from_headers(r.headers.get("Content-Type", ""), full_url)
                fname = f"{_md5(base_url)}.{ext}"
                (images_dir / fname).write_bytes(r.content)
                local_map[src] = f"images/{fname}"
                downloaded += 1
        except Exception:
            # 单张图片失败不阻断整体，保留原 URL
            continue

    # 4. 替换 Markdown 中的图片引用（字符串直接替换，兼容带标题 "Click to enlarge" 的情况）
    if local_map:
        for orig, local in local_map.items():
            text = text.replace(orig, local)
            resolved = _resolve_url(orig)
            if resolved != orig:
                text = text.replace(resolved, local)

    return text, downloaded
