"""任务模块注册表。新增模块时在此 import 并加入 MODULES 列表。"""

from .sync import SYNC_MODULE
from .ocr import OCR_MODULE
from .encheck import ENCHECK_MODULE
from .linkcheck import LINKCHECK_MODULE
from .imgnorm import IMGNORM_MODULE
from .recheck import RECHECK_MODULE

MODULES = [SYNC_MODULE, OCR_MODULE, ENCHECK_MODULE, LINKCHECK_MODULE, IMGNORM_MODULE,
           RECHECK_MODULE]

# 顶栏菜单顺序（可与 MODULES 注册顺序不同）：图片规范紧跟图片 OCR；未列出的模块按注册顺序追加末尾。
NAV_ORDER = ["sync", "ocr", "imgnorm", "encheck", "linkcheck", "recheck"]


def nav_modules() -> list[dict]:
    """按 NAV_ORDER 返回顶栏菜单用的模块列表。"""
    by_key = {m["key"]: m for m in MODULES}
    listed = set(NAV_ORDER)
    return [by_key[k] for k in NAV_ORDER if k in by_key] + \
           [m for m in MODULES if m["key"] not in listed]


# 首页分组（用户口径 2026-09）：同步（拉数据）→ 检查（四类问题）→ 复核（看修没修好）。
# 仅影响首页卡片的分组展示；顶栏顺序由 NAV_ORDER 控制。
HOME_GROUPS = [
    ("数据同步", ["sync"]),
    ("日常检查", ["encheck", "ocr", "imgnorm", "linkcheck"]),
    ("结果复核", ["recheck"]),
]


def home_groups() -> list[tuple[str, list[dict]]]:
    """返回 [(组名, [模块…])]，供首页分组渲染；未列出的模块兜底进「其他」。"""
    by_key = {m["key"]: m for m in MODULES}
    out: list[tuple[str, list[dict]]] = []
    listed: set[str] = set()
    for title, keys in HOME_GROUPS:
        mods = [by_key[k] for k in keys if k in by_key]
        out.append((title, mods))
        listed.update(k for k in keys if k in by_key)
    rest = [m for m in MODULES if m["key"] not in listed]
    if rest:
        out.append(("其他", rest))
    return out
