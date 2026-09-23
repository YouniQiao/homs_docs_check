"""任务模块注册表。新增模块时在此 import 并加入 MODULES 列表。"""

from .sync import SYNC_MODULE
from .ocr import OCR_MODULE
from .encheck import ENCHECK_MODULE
from .linkcheck import LINKCHECK_MODULE
from .imgnorm import IMGNORM_MODULE
from .recheck import RECHECK_MODULE
from .sysmerge import SYSMERGE_MODULE

MODULES = [SYNC_MODULE, OCR_MODULE, ENCHECK_MODULE, LINKCHECK_MODULE, IMGNORM_MODULE,
           RECHECK_MODULE, SYSMERGE_MODULE]

# 独立页（没有 runs/items 框架），但同样出现在顶栏菜单与首页分组里
EXTRA_MODULES = [
    {"key": "ignored", "name": "已忽略问题", "nav_name": "已忽略", "icon": "🚫",
     "description": "集中查看已忽略的检测结果，可就地恢复（按模块分页签）"},
]

_ALL_MODULES = MODULES + EXTRA_MODULES

# 顶栏菜单顺序（可与 MODULES 注册顺序不同）：图片规范紧跟图片 OCR；未列出的模块按注册顺序追加末尾。
# 注意：顶栏只列 MODULES（EXTRA_MODULES 如「已忽略问题」只在首页出现，不进菜单）。
NAV_ORDER = ["sync", "ocr", "imgnorm", "encheck", "linkcheck", "recheck"]


def nav_modules() -> list[dict]:
    """按 NAV_ORDER 返回顶栏菜单用的模块列表。

    不含 EXTRA_MODULES（如「已忽略问题」）；标了 `in_nav: False` 的模块也不列
    （如「当前全量问题」——用户 2026-09 要求从顶栏去掉，但仍保留在首页「结果复核」组里）。
    """
    def _ok(m: dict) -> bool:
        return m.get("in_nav") is not False

    by_key = {m["key"]: m for m in MODULES if _ok(m)}
    listed = set(NAV_ORDER)
    return [by_key[k] for k in NAV_ORDER if k in by_key] + \
           [m for m in MODULES if m["key"] not in listed and _ok(m)]


# 首页分组（用户口径 2026-09）：同步（拉数据）→ 检查（四类问题）→ 复核（看修没修好）。
# 仅影响首页卡片的分组展示；顶栏顺序由 NAV_ORDER 控制。
HOME_GROUPS = [
    # 顺序：全量问题总览放最前（同事最常看「当前有哪些问题要处理」）
    ("全量问题总览", ["recheck", "ignored"]),
    ("数据同步", ["sync"]),
    ("日常检查", ["ocr", "imgnorm", "encheck", "linkcheck"]),
    # 专项整改（用户 2026-09 新增）：后续放入的卡片按以下约定——
    #   ① 不进顶栏菜单（模块标 in_nav: False，或放 EXTRA_MODULES）
    #   ② 不接定时任务（不加 cron、不进日报）
    # 卡片陆续加入时，把 key 追加到下面这个列表即可。
    ("专项整改", ["sysmerge"]),
]


def home_groups() -> list[tuple[str, list[dict]]]:
    """返回 [(组名, [模块…])]，供首页分组渲染；未列出的模块兜底进「其他」。"""
    by_key = {m["key"]: m for m in _ALL_MODULES}
    out: list[tuple[str, list[dict]]] = []
    listed: set[str] = set()
    for title, keys in HOME_GROUPS:
        mods = [by_key[k] for k in keys if k in by_key]
        out.append((title, mods))
        listed.update(k for k in keys if k in by_key)
    rest = [m for m in _ALL_MODULES if m["key"] not in listed]
    if rest:
        out.append(("其他", rest))
    return out
