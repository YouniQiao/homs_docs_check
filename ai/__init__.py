"""docscheck 通用 AI 能力门面。

所有检查项目需要 AI 时统一调用本包，不要各自直连模型。
通道复用本机 Hermes 已配置的 provider（见项目根 .ai.env，600，已 gitignore）。
"""
from __future__ import annotations

from .cache import (cache_get, cache_key, cache_put, ensure_schema,
                    item_hash, note_get, note_put, stats)
from .client import AIError, chat, chat_json
from .config import AIConfig, load_config
from .facade import ask, classify, review, review_image
from .prompts import PROMPTS, prompt_version

__all__ = ["ask", "classify", "review", "review_image", "AIError",
           "load_config", "AIConfig", "PROMPTS", "prompt_version",
           "cache_get", "cache_put", "cache_key", "ensure_schema",
           "note_get", "note_put", "stats", "chat", "chat_json"]
