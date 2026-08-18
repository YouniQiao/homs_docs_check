#!/usr/bin/env python3
"""PaddleOCR 识别引擎：检测 + 识别，输出结构化结果。

与 skill 里的 paddle_cn_detector.py 同源，但返回结构化结果
（每行文字 + 置信度），供 OCR 检查任务和 Web 展示使用。

依赖：paddleocr 3.7 + paddlepaddle 3.2.2（3.3.x 有 MKLDNN crash）。
"""

import os

# 跳过 PaddleX 启动时的 HuggingFace 连通性检查（无代理环境会卡住）
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import re

CJK2_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def has_real_chinese(text: str) -> bool:
    """判断文本是否含真实中文（连续 2+ CJK 字符，误报率 0%）。"""
    return bool(CJK2_RE.search(text or ""))


class PaddleOcrEngine:
    """封装 PaddleOCR 的识别引擎（每进程一个实例，模型常驻）。"""

    def __init__(self, ocr_version: str = None, **kwargs):
        from paddleocr import PaddleOCR

        init_kwargs = dict(
            lang="ch",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        if ocr_version:
            init_kwargs["ocr_version"] = ocr_version
        init_kwargs.update(kwargs)
        self.ocr = PaddleOCR(**init_kwargs)

    def recognize(self, img) -> dict:
        """识别图片，返回结构化结果：

        {"text": 全部文本（空格拼接）, "lines": [{"text","confidence"}],
         "has_cn": bool, "max_confidence": float}
        """
        result = self.ocr.predict(img)
        lines = []
        for r in result:
            for txt, conf in zip(r.get("rec_texts", []), r.get("rec_scores", [])):
                lines.append({"text": txt, "confidence": round(float(conf), 4)})
        full = " ".join(l["text"] for l in lines)
        max_conf = max((l["confidence"] for l in lines), default=0.0)
        return {
            "text": full,
            "lines": lines,
            "has_cn": has_real_chinese(full),
            "max_confidence": max_conf,
        }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 ocr_engine.py <图片路径> [更多图片...]")
        sys.exit(1)
    eng = PaddleOcrEngine()
    for path in sys.argv[1:]:
        res = eng.recognize(path)
        flag = "✅ 含中文" if res["has_cn"] else "❌ 无中文"
        print(f"{flag}  {path}  (置信度 {res['max_confidence']:.3f})")
        for line in res["lines"]:
            print(f"    [{line['confidence']:.3f}] {line['text']}")
