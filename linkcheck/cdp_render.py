#!/usr/bin/python3
"""基于 CDP 的伪 404 判定：浏览器常驻 + 主动轮询，出结论即收工。

为什么不继续用 `--dump-dom`：
  华为文档站部分页面有长挂起请求，网络永远不空闲，dump-dom 会一直等到超时；
  `--virtual-time-budget` 在网络未空闲时也不推进 → 大量 unknown。

做法：
  * 每个 worker 常驻一个 chrome-headless-shell（省掉每条 1~2s 冷启动）
  * 页面加载后每 250ms 读一次 document：出现 h1.doc-title → ok；
    跳到 /404/ 或 title 含 404 → dead；到点还没有 → unknown
  * 可选屏蔽图片/字体/媒体，进一步提速

只用标准库（自带最小 WebSocket 客户端），不引入依赖。
"""
from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import socket
import struct
import subprocess
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
    "/usr/bin/chromium", "/usr/bin/google-chrome",
]

POLL_MS = 250
BLOCK = ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.svg",
         "*.woff", "*.woff2", "*.ttf", "*.mp4", "*.mp3"]


def find_bin(patterns) -> str | None:
    for pat in patterns:
        if not pat:
            continue
        hits = [p for p in glob.glob(pat) if os.path.isfile(p) and os.access(p, os.X_OK)]
        if hits:
            return sorted(hits)[-1]
    return None


def _die_with_parent() -> None:
    """子线程里安全地设置 PR_SET_PDEATHSIG：父进程没了，浏览器自动退出。"""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 15)   # PR_SET_PDEATHSIG=SIGTERM
    except Exception:
        pass


def browser_bin() -> str:
    exe = find_bin(SHELL_PATTERNS) or find_bin(CHROME_PATTERNS)
    if not exe:
        raise SystemExit("找不到 chrome-headless-shell / chrome")
    return exe


# ── 最小 WebSocket 客户端（RFC6455，够 CDP 用）────────────────────────────
class WS:
    def __init__(self, url: str, timeout: float = 15):
        if not url.startswith("ws://"):
            raise ValueError(f"仅支持 ws://：{url}")
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("ws 握手失败")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0] + b" ":
            raise IOError(f"ws 握手返回：{head[:80]!r}")
        self._rest = rest
        self.sock.settimeout(timeout)

    def _write_frame(self, opcode: int, data: bytes) -> None:
        head = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        mask = os.urandom(4)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, text: str) -> None:
        self._write_frame(0x1, text.encode())

    def _read_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            if self._rest:
                take = self._rest[: n - len(out)]
                self._rest = self._rest[len(take):]
                out += take
                continue
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise IOError("ws 连接已关闭")
            out += chunk
        return out

    def recv(self) -> str:
        parts = []
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if (b2 & 0x80) else None
            data = self._read_exact(length) if length else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:                      # ping → pong
                self._write_frame(0xA, data)
                continue
            if opcode == 0x8:
                raise IOError("ws close")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "ignore")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class Browser:
    """常驻一个浏览器实例，按需开关标签页。"""

    def __init__(self, exe: str, block_resources: bool = True):
        self.exe = exe
        self.profile = tempfile.mkdtemp(prefix="pseudo404-")
        cmd = [exe, "--remote-debugging-port=0", "--no-sandbox", "--disable-gpu",
               "--disable-dev-shm-usage", "--disable-background-networking",
               "--no-first-run", "--no-default-browser-check",
               f"--user-data-dir={self.profile}", "about:blank"]
        if "headless-shell" not in os.path.basename(exe):
            cmd.insert(1, "--headless")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=_die_with_parent)   # 父进程被杀时浏览器一起退，避免孤儿
        port_file = os.path.join(self.profile, "DevToolsActivePort")
        port, ws_path = None, ""
        for _ in range(200):                      # 最多等 10s
            try:
                with open(port_file, encoding="utf-8") as fh:
                    lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
                port = int(lines[0])
                ws_path = lines[1] if len(lines) > 1 else ""   # 形如 /devtools/browser/<uuid>
                break
            except Exception:
                time.sleep(0.05)
        if not port:
            self.close()
            raise IOError("浏览器未就绪（读不到 DevToolsActivePort）")
        if not ws_path:                           # 老版本只有端口，退回 /json/version
            import urllib.request
            info = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=5).read())
            ws_path = info["webSocketDebuggerUrl"].split(str(port), 1)[1]
        self.ws = WS(f"ws://127.0.0.1:{port}{ws_path}")
        self._id = 0
        self.block = block_resources

    def call(self, method: str, params: dict | None = None,
             session_id: str | None = None, timeout: float = 20) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.sock.settimeout(timeout)
        self.ws.send(json.dumps(msg))
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == self._id:
                if "error" in resp:
                    raise IOError(f"{method}: {resp['error']}")
                return resp.get("result", {})

    _PROBE = ("JSON.stringify({p:location.pathname,t:document.title,"
              "h:(document.querySelector('h1.doc-title')||{}).innerText||''})")

    def render(self, url: str, timeout: float = 25) -> tuple[str, float]:
        """返回 (verdict, 耗时秒)。verdict: ok | dead | unknown"""
        t0 = time.time()
        tid = sid = None
        try:
            tid = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            sid = self.call("Target.attachToTarget",
                            {"targetId": tid, "flatten": True})["sessionId"]
            if self.block:
                self.call("Network.enable", {}, sid)
                self.call("Network.setBlockedURLs", {"urls": BLOCK}, sid)
            self.call("Page.navigate", {"url": url}, sid, timeout=timeout)
            verdict = "unknown"
            while time.time() - t0 < timeout:
                time.sleep(POLL_MS / 1000)
                try:
                    res = self.call("Runtime.evaluate",
                                    {"expression": self._PROBE, "returnByValue": True},
                                    sid, timeout=5)
                except Exception:
                    continue
                raw = (res.get("result") or {}).get("value")
                if not raw:
                    continue
                st = json.loads(raw)
                if st.get("h"):                                  # 有正文标题 → 真文档
                    verdict = "ok"
                    break
                if "/404/" in (st.get("p") or "") or "404" in (st.get("t") or ""):
                    verdict = "dead"
                    break
            return verdict, round(time.time() - t0, 1)
        except Exception:
            return "unknown", round(time.time() - t0, 1)
        finally:
            if tid:
                try:
                    self.call("Target.closeTarget", {"targetId": tid}, timeout=5)
                except Exception:
                    pass

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)


def render_batch(urls: list[str], workers: int = 8, timeout: float = 25,
                 block_resources: bool = True, progress=None) -> list[dict]:
    """多线程渲染：每个线程一个常驻浏览器，URL 静态切片。"""
    exe = browser_bin()
    buckets: list[list[str]] = [[] for _ in range(max(1, workers))]
    for i, u in enumerate(urls):
        buckets[i % len(buckets)].append(u)

    out: list[dict] = []
    lock_owner = {}

    def work(chunk: list[str]) -> list[dict]:
        if not chunk:
            return []
        br = Browser(exe, block_resources=block_resources)
        res = []
        try:
            for u in chunk:
                v, sec = br.render(u, timeout=timeout)
                res.append({"url": u, "verdict": v, "sec": sec})
                if progress:
                    progress(res[-1])
        finally:
            br.close()
        return res

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=len(buckets)) as pool:
        for chunk_res in pool.map(work, buckets):
            out.extend(chunk_res)
    return out


def main() -> None:
    import sys
    if len(sys.argv) < 3:
        raise SystemExit("用法: cdp_render.py <urls.json|urls.txt> <out.jsonl> [workers] [timeout]")
    src, dst = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    timeout = float(sys.argv[4]) if len(sys.argv) > 4 else 25
    raw = open(src, encoding="utf-8").read().strip()
    urls = json.loads(raw) if raw.startswith("[") else [l.strip() for l in raw.splitlines() if l.strip()]
    print(f"引擎 {browser_bin()}\n{len(urls)} 条 · {workers} 并发 · 超时 {timeout:g}s", flush=True)
    t0 = time.time()
    done = 0
    fh = open(dst, "w", encoding="utf-8")

    def prog(r):
        nonlocal done
        done += 1
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        fh.flush()
        if done % 20 == 0:
            el = time.time() - t0
            print(f"进度 {done}/{len(urls)}  {r['verdict']}  ({el:.0f}s, {el/done:.1f}s/条)", flush=True)

    res = render_batch(urls, workers=workers, timeout=timeout, progress=prog)
    fh.close()
    import collections
    print("完成", dict(collections.Counter(r["verdict"] for r in res)),
          f"{time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
