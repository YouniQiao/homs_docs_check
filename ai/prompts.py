"""版本化提示词。改动任一条即升 version（缓存随 key 自动失效）。"""
from __future__ import annotations

PROMPTS: dict[str, dict] = {
    "ocr_false_positive": {
        "version": "v1",
        "system": (
            "你是华为鸿蒙开发者文档的本地化质量检查员。"
            "判断英文文档截图中的中文，是否属于「本应翻译成英文却仍是中文」的漏翻缺陷。\n"
            "属于缺陷(real)：界面文案/标题/按钮/菜单/正文/提示语本应英文却为中文。\n"
            "不属于缺陷(false_positive)：代码、命令行、示例中的中文；中文品牌/专有名词；"
            "被 OCR 误识别的形近字；中英文混排且属正常的术语。"
        ),
        "user": (
            "文档地址：{doc_url}\n"
            "OCR 识别到的文字：{ocr_text}\n"
            "低置信度行：{low_conf}\n\n"
            "请结合截图判断是否存在漏翻缺陷。只输出 JSON：\n"
            '{{"verdict":"real|false_positive|uncertain","confidence":0.0-1.0,'
            '"reason":"50字内说明","suggestion":"若为 real，20字内整改建议，否则空字符串"}}'
        ),
    },
}


def prompt_version(rubric: str) -> str:
    return PROMPTS.get(rubric, {}).get("version", "v0")
