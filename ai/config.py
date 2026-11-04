"""读取 AI 配置：项目根 .ai.env（600，gitignore）。"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".ai.env"


def _read_env(path: pathlib.Path) -> dict:
    d: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return d


@dataclass
class AIConfig:
    base_url: str = ""
    api_key: str = ""
    model_fast: str = "deepseek-v4.1-flash"
    model_smart: str = "deepseek-v4.1-flash"
    extra_headers: dict = field(default_factory=dict)
    timeout: float = 60.0
    max_retries: int = 3
    db_path: str = str(ROOT / "index.db")

    def model(self, tier: str = "fast") -> str:
        return self.model_smart if tier == "smart" else self.model_fast

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


_cfg: AIConfig | None = None
_ENV_SIG: str = ""


def _env_signature() -> str:
    """`.ai.env` 内容哈希——用它判断是否需要重载（比 mtime 稳：同秒重写也能察觉）。"""
    import hashlib
    try:
        return hashlib.sha256(ENV_FILE.read_bytes()).hexdigest()
    except OSError:
        return ""


def key_fingerprint() -> str:
    """当前 key 的短指纹（用于缓存归属判断；不泄露 key）。"""
    import hashlib
    return hashlib.sha256(load_config().api_key.encode()).hexdigest()[:12]


def load_config(reload: bool = False) -> AIConfig:
    """读 .ai.env 并缓存；**文件 mtime 变了会自动重载**（改 key 无需重启）。"""
    global _cfg, _ENV_SIG
    sig = _env_signature()
    if _cfg is not None and not reload and sig == _ENV_SIG:
        return _cfg
    e = _read_env(ENV_FILE)
    headers: dict = {}
    raw = e.get("AI_EXTRA_HEADERS", "")
    if raw:
        try:
            headers = json.loads(raw)
        except Exception:
            headers = {}
    _cfg = AIConfig(
        base_url=e.get("AI_BASE_URL", "").rstrip("/"),
        api_key=e.get("AI_API_KEY", ""),
        model_fast=e.get("AI_MODEL_FAST", "deepseek-v4.1-flash"),
        model_smart=e.get("AI_MODEL_SMART", e.get("AI_MODEL_FAST", "deepseek-v4.1-flash")),
        extra_headers=headers,
        timeout=float(e.get("AI_TIMEOUT", "60")),
        max_retries=int(e.get("AI_MAX_RETRIES", "3")),
    )
    _ENV_SIG = sig
    return _cfg
