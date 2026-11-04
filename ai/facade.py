"""对外门面：各类检查项目统一调这几个函数。"""
from __future__ import annotations

import base64
import json
import pathlib

from .cache import (cache_get, cache_key, cache_put, ensure_schema, item_hash)
from .client import AIError, chat, chat_json
from .config import load_config
from .prompts import PROMPTS, prompt_version


def ask(question: str, tier: str = "fast", system: str | None = None,
        max_tokens: int = 1400) -> str:
    """自由问答（探索/调试用）。"""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": question})
    content, _ = chat(msgs, load_config().model(tier), max_tokens=max_tokens)
    return content


def _img_part(path: str) -> dict:
    p = pathlib.Path(path)
    suffix = p.suffix.lower().lstrip(".") or "jpeg"
    mime = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
    b64 = base64.b64encode(p.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def review(rubric: str, fields: dict, images: list[str] | None = None,
           *, module: str = "", item_key: str = "", tier: str = "fast",
           use_cache: bool = True, max_tokens: int = 2000) -> dict:
    """按 rubric（提示词名）对单条做结构化判定，返回 dict（含 _meta）。"""
    spec = PROMPTS.get(rubric)
    if not spec:
        raise AIError(f"未知 rubric: {rubric}")
    ensure_schema()
    cfg = load_config()
    model = cfg.model(tier)
    pver = spec["version"]

    # 缓存键：含图片内容哈希，图变了自动失效
    imgs_meta = []
    for p in (images or []):
        p = str(p)
        imgs_meta.append({"path": p, "hash": item_hash({"n": None}) if not pathlib.Path(p).exists()
                          else __import__("hashlib").sha256(pathlib.Path(p).read_bytes()).hexdigest()})
    key = cache_key(rubric, model, pver, {"fields": fields, "images": imgs_meta})

    if use_cache:
        hit = cache_get(key)
        if hit is not None:
            hit = dict(hit)
            hit["_meta"] = {"cached": True, "model": model, "prompt_version": pver}
            return hit

    user_txt = spec["user"].format(**{k: (fields.get(k) or "") for k in
                                      ("doc_url", "ocr_text", "low_conf")}) \
        if "{" in spec["user"] else spec["user"]
    content = [{"type": "text", "text": user_txt}]
    for p in (images or []):
        content.append(_img_part(str(p)))
    msgs = [{"role": "system", "content": spec["system"]},
            {"role": "user", "content": content}]

    out, usage = chat_json(msgs, model, max_tokens=max_tokens)
    out = {"verdict": out.get("verdict", "uncertain"),
           "confidence": out.get("confidence"),
           "reason": (out.get("reason") or "").strip(),
           "suggestion": (out.get("suggestion") or "").strip()}
    if use_cache:
        cache_put(key, rubric, model, pver, item_hash({"fields": fields, "images": imgs_meta}), out, usage)
    out["_meta"] = {"cached": False, "model": model, "prompt_version": pver,
                    "tokens": usage.get("completion_tokens")}
    return out


def review_image(rubric: str, fields: dict, image: str, **kw) -> dict:
    return review(rubric, fields, images=[image], **kw)


def classify(items: list, schema_hint: str, *, batch: int = 20,
             tier: "str" = "fast", system: str = "") -> dict:
    """批量结构化分类占位（P3 用）。items: [{"key":..., "text":...}]"""
    raise NotImplementedError("classify 待 P3 实现")
