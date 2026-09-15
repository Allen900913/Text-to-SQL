# -*- coding: utf-8 -*-
"""換機驗收：新電腦搬完之後跑這一支，四項全過才算搬好。

為什麼不直接跑 check_table_retrievability.py 來驗向量快取：
那支每張表都要嵌入一次查詢，實測 > 2 分鐘而且真的打 API ——
換機當下 429 還沒解除，用它來「確認不用打 API」本身就是在打 API。

快取是以 doc_hash 為鍵的（table_retriever.get_table_vectors、
column_hints.get_column_vectors 都一樣），所以「會不會重嵌」不必跑，
算一次雜湊就知道。這支零 API 呼叫。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from loguru import logger as log  # noqa: E402

# 舊機實測的基準（2026-09-15）。搬完必須逐項相同 —— 這些數字是 GT 的前提，
# 對不上就不要跑評估，因為模型錯與資料錯會混在一起（[[gt-before-test]]）。
EXPECT = {"tables": 93, "rows": 10213, "enum_cols": 98, "commented_cols": 1071,
          "table_comments": 93, "indexes": 196, "foreign_keys": 105}


def check_db() -> list[str]:
    from sqlalchemy import text

    from langgraph_sql.config import MYSQL_URI
    from langgraph_sql.utils.db_manager import get_db_manager

    q = {
        "tables": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()",
        "enum_cols": "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND data_type='enum'",
        "commented_cols": "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND column_comment<>''",
        "table_comments": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_comment<>''",
        "indexes": "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=DATABASE()",
        "foreign_keys": "SELECT COUNT(*) FROM information_schema.key_column_usage WHERE table_schema=DATABASE() AND referenced_table_name IS NOT NULL",
    }
    bad = []
    with get_db_manager(MYSQL_URI).engine.connect() as c:
        names = [r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE()"))]
        # information_schema.table_rows 對 InnoDB 是估計值，不能拿來當驗收。
        got = {"rows": sum(c.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar() for t in names)}
        for k, sql in q.items():
            got[k] = c.execute(text(sql)).scalar()
    for k, want in EXPECT.items():
        ok = got[k] == want
        print(f"  {'OK  ' if ok else 'X   '}{k:16s} {got[k]:>6}  (預期 {want})")
        if not ok:
            bad.append(f"{k}: {got[k]} != {want}")
    return bad


def check_caches() -> list[str]:
    """零 API：只算雜湊，不呼叫 embed。"""
    import json

    from langgraph_sql.utils.embedding import doc_hash
    bad = []

    from langgraph_sql.utils import table_retriever as tr
    docs = tr.build_table_documents()
    cache = json.load(open(tr._CACHE_PATH, encoding="utf-8")) if os.path.exists(tr._CACHE_PATH) else {}
    stale = [t for t, d in docs.items() if doc_hash(d) not in cache]
    print(f"  {'OK  ' if not stale else 'X   '}表向量      {len(docs)-len(stale)}/{len(docs)} 命中快取")
    if stale:
        bad.append(f"表向量要重嵌 {len(stale)} 張")

    from langgraph_sql.utils import column_hints as ch
    cdocs, _ = ch._load()
    ccache = json.load(open(ch._CACHE_PATH, encoding="utf-8")) if os.path.exists(ch._CACHE_PATH) else {}
    cstale = [k for k in cdocs if doc_hash(cdocs[k]) not in ccache]
    print(f"  {'OK  ' if not cstale else 'X   '}欄位提示向量 {len(cdocs)-len(cstale)}/{len(cdocs)} 命中快取")
    if cstale:
        bad.append(f"欄位向量要重嵌 {len(cstale)} 個")
    return bad


def check_files() -> list[str]:
    bad = []
    for p, why in [(".env", "API 金鑰"),
                   ("eval_ground_truth.yaml", "GT"),
                   ("eval_questions_v2.json", "開發集"),
                   ("eval/testset_holdout.yaml", "保留驗收集")]:
        ok = os.path.exists(os.path.join(_ROOT, p))
        print(f"  {'OK  ' if ok else 'X   '}{p:28s} {why}")
        if not ok:
            bad.append(f"缺 {p}")
    n = len([f for f in os.listdir(os.path.join(_ROOT, "eval", "results"))]) \
        if os.path.isdir(os.path.join(_ROOT, "eval", "results")) else 0
    print(f"  {'OK  ' if n else '!   '}eval/results/{'':16s} {n} 個存檔"
          + ("" if n else "  ← 離線探針 replay 不了，但不影響跑評估"))

    from langgraph_sql.config import NVIDIA_API_KEY
    ok = bool(NVIDIA_API_KEY)
    print(f"  {'OK  ' if ok else 'X   '}{'NVIDIA_API_KEY':28s} {'已載入' if ok else '空的 —— 嵌入會失敗'}")
    if not ok:
        bad.append("NVIDIA_API_KEY 沒載入")
    return bad


def main() -> int:
    bad = []
    print("\n[1] 資料庫形狀")
    bad += check_db()
    print("\n[2] 向量快取（零 API 呼叫）")
    bad += check_caches()
    print("\n[3] 必要檔案")
    bad += check_files()

    print("\n" + "=" * 62)
    if bad:
        print(f"{len(bad)} 項不符，先修完再跑評估：")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("三項全過：資料庫與舊機逐項相同，快取全命中，金鑰已載入。")
    print("接著跑 tools/check_schema_pipeline.py（八項閘門）就可以開始工作。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
