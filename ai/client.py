"""OpenAI 兼容的底层调用（带重试/超时）。

⚠️ 本机通道（opencode-go / deepseek-v4.1-flash）是**推理模型**：
   reasoning 会吃掉大量 token，因此 max_tokens 必须给足（经验值 ≥1200）。
"""
from __future__ import annotations

import json
import re
import time

import requests

from .config import load_config


class AIError(RuntimeError):
    pass


def chat(messages: list, model: str, max_tokens: int = 1400,
         temperature: float = 0.0) -> tuple[str, dict]:
    """返回 (content, usage)。messages 为 OpenAI 格式。"""
    cfg = load_config()
    if not cfg.configured:
        raise AIError("AI 未配置：缺 .ai.env 的 AI_BASE_URL / AI_API_KEY")
    url = f"{cfg.base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json", **cfg.extra_headers}
    payload = {"model": model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    last: Exception | None = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=cfg.timeout)
            if r.status_code >= 400:
                raise AIError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            return data["choices"][0]["message"].get("content") or "", data.get("usage", {})
        except Exception as ex:  # noqa: BLE001
            last = ex
            if attempt < cfg.max_retries:
                time.sleep(min(2 ** attempt, 8))
    raise AIError(f"调用失败（{cfg.max_retries} 次）：{last}")


def _extract_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 包裹/前后废话）。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise AIError(f"输出里没有 JSON：{t[:200]}")
    return json.loads(t[i:j + 1])


def chat_json(messages: list, model: str, max_tokens: int = 2000,
              temperature: float = 0.0,
              # 解析失败时逐次加大预算重试（推理模型偶尔把 token 全花在 reasoning 上）
              retries: int = 3) -> tuple[dict, dict]:
    """要求返回 JSON，自动解析；解析失败自动加大 max_tokens 重试。返回 (dict, usage)。"""
    budget = max_tokens
    last: Exception | None = None
    for _ in range(retries):
        content, usage = chat(messages, model, max_tokens=budget, temperature=temperature)
        try:
            return _extract_json(content), usage
        except AIError as ex:
            last = ex
            budget = int(budget * 1.6)
    raise AIError(f"JSON 解析失败（已重试 {retries} 次，末次预算 {budget}）：{last}")
