"""任务模块注册表。新增模块时在此 import 并加入 MODULES 列表。"""

from .sync import SYNC_MODULE
from .ocr import OCR_MODULE
from .encheck import ENCHECK_MODULE
from .linkcheck import LINKCHECK_MODULE

MODULES = [SYNC_MODULE, OCR_MODULE, ENCHECK_MODULE, LINKCHECK_MODULE]
