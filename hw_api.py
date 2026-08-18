"""华为开发者文档非公开后端 API 封装。

端点：
  POST .../documentPortal/getCatalogTree   获取目录树
  POST .../documentPortal/getDocumentById  获取单篇文档

必带 Referer 头，否则被拒。
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

BASE = "https://svc-drcn.developer.huawei.com/community/servlet/consumer/cn/documentPortal/"

HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://developer.huawei.com/consumer/en/doc/",
    "User-Agent": "Mozilla/5.0",
}


class HuaweiAPIError(Exception):
    pass


def _post(method: str, payload: dict, timeout: int = 30,
          retries: int = 3, backoff: float = 2.0) -> dict:
    """POST 到 API，带重试。返回解析后的 JSON（value 部分已校验 code==0）。"""
    url = BASE + method
    body = json.dumps(payload).encode("utf-8")

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw)
            code = data.get("code")
            if code == 0:
                return data
            # 业务错误码（如 92520013 无效 catalog、92511002 参数错）
            if code in (92511002, 92520013):
                raise HuaweiAPIError(f"API 业务错误 code={code} message={data.get('message')} payload={payload}")
            # 其他错误（疑似限流），退避重试
            last_err = HuaweiAPIError(f"API code={code} message={data.get('message')}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
        except HuaweiAPIError:
            raise
        time.sleep(backoff ** attempt)

    raise HuaweiAPIError(f"请求失败（重试 {retries} 次后）: {last_err}")


def get_catalog_tree(catalog: str, lang: str) -> dict:
    """获取目录树。返回 {catalogTreeList: [...]}。"""
    data = _post("getCatalogTree", {"catalogName": catalog, "language": lang})
    return data.get("value", {})


def get_document(object_id: str, catalog: str, lang: str) -> dict:
    """获取单篇文档。返回 value 字典（含 title/content/updatedDate/fileName 等）。"""
    data = _post("getDocumentById", {
        "objectId": object_id,
        "catalog": catalog,
        "language": lang,
    })
    return data.get("value", {})


def collect_documents(tree: dict) -> list[dict]:
    """从目录树递归收集所有叶子文档节点。

    返回 [{relate_document, title}]。
    """
    docs: list[dict] = []

    def walk(nodes):
        for n in nodes:
            if n.get("isLeaf") and n.get("relateDocument"):
                docs.append({
                    "relate_document": n["relateDocument"],
                    "title": n.get("nodeName", ""),
                })
            if n.get("children"):
                walk(n["children"])

    for root in tree.get("catalogTreeList", []):
        walk(root.get("children", []))
    return docs
