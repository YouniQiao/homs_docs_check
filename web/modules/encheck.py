"""模块：英文文档检查（中文字符 + 中文链接）。

执行脚本：encheck/en_check.py（--full 全量 / 默认增量）。
每篇文档一个 item：item_type = problem / clean / error；
类型列按 multi_badge 四项独立计数渲染多标签（含汉字 / 含标点 / 链接URL含中文 / 含中文链接），
不存在"两者都有"这种组合类型。
detail: doc_key, title, doc_url, catalog,
        hanzi, hanzi_count, punct, punct_count,
        url_cn_chars, url_cn_char_count, url_cn_links, cn_links, cn_link_count
"""


ENCHECK_MODULE = {
    "key": "encheck",
    "name": "英文文档检查",
    "icon": "🌐",
    "description": "检查英文文档中的中文字符与中文跳转链接",
    "summary_fields": [("total", "检查文档"), ("hanzi", "含汉字"),
                       ("cn_link", "含中文链接")],
    "detail_summary_fields": [("total", "检查文档"), ("hanzi", "含汉字"),
                              ("punct", "含标点"),
                              ("url_cn", "链接URL含中文"),
                              ("cn_link", "含中文链接"),
                              ("errors", "错误")],
    "item_columns": [("catalog", "分类"),
                     ("hanzi", "中文汉字"),
                     ("punct", "中文标点"),
                     ("url_cn_links", "链接URL中文", "link_list"),
                     ("cn_links", "中文链接", "link_list"),
                     ("url", "源文档")],
    "filters": [
        {"key": "type", "label": "问题", "source": "count",
         "fields": {"hanzi": "hanzi_count", "punct": "punct_count",
                    "url_cn": "url_cn_char_count", "cn_link": "cn_link_count"},
         "options": [("all", "全部"), ("hanzi", "含汉字"),
                     ("punct", "含标点"), ("url_cn", "链接URL含中文"),
                     ("cn_link", "含中文链接")]},
        {"key": "catalog", "label": "分类", "source": "detail",
         "options": [("all", "全部"), ("harmonyos-guides", "guides"),
                     ("harmonyos-references", "references"),
                     ("harmonyos-faqs", "faqs"),
                     ("harmonyos-releases", "releases"),
                     ("best-practices", "best-practices")]},
        {"key": "dl", "label": "源文档地址包含", "source": "contains",
         "field": "doc_url", "control": "text",
         "placeholder": "如 best-practices 或 avplayer"},
    ],
    # 类型列多标签渲染：(detail 计数字段, badge 样式, 标签文本)；全为 0 显示"正常"
    "multi_badge": [
        ("hanzi_count", "badge-failed", "含汉字"),
        ("punct_count", "badge-modified", "含标点"),
        ("url_cn_char_count", "badge-info", "链接URL含中文"),
        ("cn_link_count", "badge-added", "含中文链接"),
    ],
    "badge_map": {
        "problem": ("badge-modified", "含中文"),
        "clean": ("badge-added", "正常"),
        "error": ("badge-failed", "读取失败"),
    },
}
