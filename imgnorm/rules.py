"""图片内容规范检查：规则集。

输入是 OCR 已识别的文本（items.detail_json.ocr_text），不重跑 OCR。
每条规则一个 id，同时作为 item.detail 里的计数字段（n_<id>），供 multi_badge / 筛选使用。

设计要点：
- 归一化先行：剥 URL、剥 import 路径（@ohos.xxx / @kit.Xxx），避免把代码里的类名当术语错误。
- 繁体字判定用 OpenCC 繁→简映射表（data/ts.txt），只认"简繁不同形"的字，杜绝同形字误报。
- 阈值化：繁体字需 >=2 个不同字；占位 "xxx" 需 >=3 个 x；"中文图含英文"排除代码截图。
"""

from __future__ import annotations

import re
from pathlib import Path

RULE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 归一化
_URL_RE = re.compile(r"https?://\S+|www\.[^\s]+", re.I)
_IMPORT_RE = re.compile(r"@(?:ohos|kit|hms|arkts|system|app)\.[\w.]+", re.I)


def strip_urls(text: str) -> str:
    """剥掉 URL，避免 URL 里的产品名/路径触发术语与占位规则。"""
    return _URL_RE.sub(" ", text or "")


def _clean(text: str) -> str:
    """剥 URL + 剥 import 模块路径（@ohos.xxx / @kit.Xxx）。"""
    return _IMPORT_RE.sub(" ", strip_urls(text))


# ---------------------------------------------------------------- 繁体字表（OpenCC TSCharacters）
def _load_trad_only() -> set:
    """从 OpenCC TSCharacters.txt 构建"繁体独有"字集：存在不同于自身的简体对应字。"""
    path = RULE_DIR / "data" / "ts.txt"
    chars: set = set()
    if not path.exists():
        return chars
    with open(path, encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            trad = parts[0]
            variants = parts[1:]
            if any(v != trad for v in variants):
                chars.add(trad)
    return chars


TRAD_ONLY = _load_trad_only()

# ---------------------------------------------------------------- 术语 / 大小写
# (显示文本, 正则)。仅收录"错误写法"，正确写法不列。
TERM_PATTERNS = [
    ("harmonyos", re.compile(r"\bharmonyos\b")),
    ("Harmony OS", re.compile(r"Harmony\s+OS\b")),
    ("arkts", re.compile(r"\barkts\b")),
    ("Arkts", re.compile(r"\bArkts\b")),
    ("ARKTS", re.compile(r"\bARKTS\b")),
    ("openharmony", re.compile(r"\bopenharmony\b")),
    ("arkui", re.compile(r"\barkui\b")),
    ("Arkui", re.compile(r"\bArkui\b")),
    ("deveco", re.compile(r"\bdeveco\b")),
    ("DevEcoStudio", re.compile(r"DevEcoStudio\b")),
    ("DevEco studio", re.compile(r"DevEco\s+studio\b")),
    ("OHPM", re.compile(r"\bOHPM\b")),
]

# ---------------------------------------------------------------- 安全 / 隐私
SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|bearer|api[_-]?key|access[_-]?key"
    r"|app[_-]?secret|client[_-]?secret|private[_-]?key)\b\s*[:=]")
IP_RE = re.compile(r"(?<![\d.])(?:10|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 排除：示例域名 + 代码里的模块路径（xxx@ohos.app / @kit.xxx）
EMAIL_SAFE_RE = re.compile(
    r"@(example\.(com|org|net)|test\.(com|org|net)|xxx\.com|abc\.com|company\.com"
    r"|email\.com|yourmail\.com|sample\.com|ohos\.|kit\.|hms\.|arkts\.|system\.|app\.)",
    re.I)

# ---------------------------------------------------------------- 水印 / 内部字样
WATERMARK_RE = re.compile(
    r"内部资料|内部使用|仅供内部|禁止外传|请勿外传|严禁外传|仅限内部|保密|机密"
    r"|未经授权|confidential|internal\s+use\s+only", re.I)

# ---------------------------------------------------------------- 占位 / 测试残留
PLACEHOLDER_RE = re.compile(
    r"\bTODO\b|\bFIXME\b|\bTBD\b|x{3,}|lorem\s+ipsum|未命名|新建文件夹|张三|李四|王五"
    r"|13800138000|example\.com|待补充|请填写|此处填写", re.I)

# ---------------------------------------------------------------- 规则注册表
RULES = [
    {"id": "term", "label": "术语/大小写", "cls": "badge-modified",
     "kind": "patterns", "patterns": TERM_PATTERNS},
    {"id": "secret", "label": "疑似密钥", "cls": "badge-failed",
     "kind": "regex", "rx": SECRET_RE},
    {"id": "ip", "label": "内网IP", "cls": "badge-failed",
     "kind": "regex", "rx": IP_RE},
    {"id": "phone", "label": "手机号", "cls": "badge-failed",
     "kind": "regex", "rx": PHONE_RE},
    {"id": "email", "label": "邮箱", "cls": "badge-info",
     "kind": "regex", "rx": EMAIL_RE, "exclude": EMAIL_SAFE_RE},
    {"id": "watermark", "label": "水印/内部字样", "cls": "badge-deleted",
     "kind": "regex", "rx": WATERMARK_RE},
    {"id": "placeholder", "label": "占位/测试残留", "cls": "badge-modified",
     "kind": "regex", "rx": PLACEHOLDER_RE},
    {"id": "trad", "label": "繁体字", "cls": "badge-info", "kind": "trad"},
    {"id": "reverse_en", "label": "中文图含英文", "cls": "badge-info",
     "kind": "reverse_en"},
    {"id": "lowq", "label": "低清/模糊", "cls": "badge-deleted", "kind": "lowq"},
]

FIELD = {r["id"]: "n_" + r["id"] for r in RULES}
LABEL = {r["id"]: r["label"] for r in RULES}
BADGE_CLS = {r["id"]: r["cls"] for r in RULES}

LOWQ_THRESHOLD = 0.6          # 整图最高置信度低于此值 → 判定低清/模糊
REVERSE_EN_MIN_EN = 60        # 中文文档图中英文字符数下限
REVERSE_EN_RATIO = 3.0        # 英文数 > 中文数 * 该倍数
CODE_HINT_RE = re.compile(r"[{}();=<>\[\]]")   # 代码截图特征字符
CODE_HINT_MIN = 8             # 代码特征字符数达到此值 → 视为代码截图，跳过 reverse_en
EVIDENCE_MAX = 3


def _cap(n: int, hi: int = 99) -> int:
    return min(n, hi)


def _uniq(items: list, limit: int = EVIDENCE_MAX) -> list:
    out: list = []
    for m in items:
        if m not in out:
            out.append(m)
        if len(out) >= limit:
            break
    return out


def _looks_like_code(text: str) -> bool:
    return len(CODE_HINT_RE.findall(text)) >= CODE_HINT_MIN


def analyze(text: str, lang: str, confidence: float) -> tuple[dict, dict, str]:
    """对一段 OCR 文本跑全部规则。

    返回 (counts, evidence, evidence_text)。
    """
    raw = text or ""
    clean = _clean(raw)
    counts: dict = {}
    evidence: dict = {}

    for rule in RULES:
        rid = rule["id"]
        field = FIELD[rid]
        kind = rule["kind"]

        if kind == "patterns":
            hits: list[str] = []
            for label, rx in rule["patterns"]:
                for m in rx.finditer(clean):
                    s, e = m.span()
                    # 排除 import / 命名空间路径里的片段：@ohos.arkui.X、arkui.UIContext
                    if s > 0 and clean[s - 1] == "@":
                        continue
                    if e < len(clean) and clean[e] in "._/":
                        continue
                    hits.append(label)
            if hits:
                counts[field] = _cap(len(hits))
                evidence[field] = _uniq(hits)
        elif kind == "regex":
            found = rule["rx"].findall(clean)
            found = [f if isinstance(f, str) else next((x for x in f if x), "")
                     for f in found]
            if "exclude" in rule:
                found = [f for f in found if not rule["exclude"].search(f)]
            if found:
                counts[field] = _cap(len(found))
                evidence[field] = _uniq(found)
        elif kind == "trad":
            chars = [c for c in clean if c in TRAD_ONLY]
            if len(set(chars)) >= 2:
                counts[field] = _cap(len(set(chars)))
                evidence[field] = _uniq(sorted(set(chars)), 8)
        elif kind == "reverse_en":
            if lang == "cn" and not _looks_like_code(clean):
                en = sum(1 for c in clean if c.isascii() and c.isalpha())
                cn = sum(1 for c in clean if "\u4e00" <= c <= "\u9fff")
                if en >= REVERSE_EN_MIN_EN and en > cn * REVERSE_EN_RATIO:
                    counts[field] = en
                    evidence[field] = [clean.strip()[:60]]
        elif kind == "lowq":
            if confidence and confidence < LOWQ_THRESHOLD:
                counts[field] = 1
                evidence[field] = [f"max_conf={confidence:.3f}"]

    parts = []
    for rule in RULES:
        field = FIELD[rule["id"]]
        if evidence.get(field):
            parts.append(f"{rule['label']}: " + ", ".join(evidence[field]))
    ev_text = " | ".join(parts)[:300]
    return counts, evidence, ev_text
