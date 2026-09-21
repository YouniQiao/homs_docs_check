"""一次性回填 doc_anchors：为每篇文档采集源 HTML 的 id=（站点真实锚点）。

可中断续跑（已采的不重复）。只读 API，温和并发；写库在主线程串行。
"""
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, "/opt/projects/harmonyos_docs")
from hw_api import get_document
from db import IndexDB

BASE = Path("/opt/projects/harmonyos_docs")
db = IndexDB(str(BASE / "index.db"))

rows = db._conn.execute(
    "SELECT doc_key, relate_document, catalog, lang FROM docs").fetchall()
have = {r[0] for r in db._conn.execute("SELECT DISTINCT doc_key FROM doc_anchors")}
if "--force" in sys.argv:
    have = set()
    print("--force：全量重采", flush=True)
todo = [r for r in rows if r[0] not in have]
print(f"总文档 {len(rows)} | 已采 {len(have)} | 待采 {len(todo)}", flush=True)

def fetch(row):
    dk, rel, cat, lang = row
    try:
        v = get_document(rel, cat, lang)
        html = (v.get("content") or {}).get("content", "")
        if not html:
            return dk, None
        return dk, sorted(set(re.findall(r'id="([^"]+)"', html))
                          | set(re.findall(r'<a[^>]+name="([^"]+)"', html)))
    except Exception as e:
        return dk, e

t0 = time.time()
ok = fail = 0
fails = []
with ThreadPoolExecutor(max_workers=6) as ex:
    futs = {ex.submit(fetch, r): r[0] for r in todo}
    for i, fut in enumerate(as_completed(futs), 1):
        dk, res = fut.result()
        if isinstance(res, Exception):
            fail += 1
            fails.append((dk, repr(res)[:80]))
        else:
            db.set_doc_anchors(dk, res or [])
            ok += 1
        if i % 200 == 0:
            db.commit()
            el = time.time() - t0
            print(f"  {i}/{len(todo)} | ok={ok} fail={fail} | {el:.0f}s "
                  f"({i/el:.1f}/s) | ETA {(len(todo)-i)/max(i/el,0.01)/60:.0f}min", flush=True)
db.commit()
print(f"✅ 回填完成 ok={ok} fail={fail} 用时 {time.time()-t0:.0f}s", flush=True)
for dk, e in fails[:20]:
    print("   失败:", dk, e)
n_now = db._conn.execute("SELECT COUNT(DISTINCT doc_key) FROM doc_anchors").fetchone()[0]
n_rows = db._conn.execute("SELECT COUNT(*) FROM doc_anchors").fetchone()[0]
print(f"库中现有：{n_now} 篇 / {n_rows} 条锚点", flush=True)
db.close()
