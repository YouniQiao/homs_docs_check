"""文档「分类」（docs.catalog）的唯一权威定义——所有下拉/标签必须引用这里，别各写各的。"""

# 顺序 = 界面显示顺序（指南 → API参考 → FAQ → 版本说明 → 最佳实践）
CATALOG_LABELS = {
    "harmonyos-guides": "指南",
    "harmonyos-references": "API 参考",
    "harmonyos-faqs": "FAQ",
    "harmonyos-releases": "版本说明",
    "best-practices": "最佳实践",
}

# 过滤下拉用：「全部」+ 各分类
CATALOG_OPTIONS = [("all", "全部")] + [(k, v) for k, v in CATALOG_LABELS.items()]
