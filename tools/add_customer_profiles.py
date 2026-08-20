"""
就地新增 customer_profiles 寬表 —— 不動現有 21 張表一個位元。

為什麼不直接重跑 init_db.py：那會 TRUNCATE 並重建整個資料庫。雖然種子固定、
結果理論上相同，但那是破壞性操作，而這裡要的只是「多一張表」。

安全性由三件事保證：
  1. CREATE TABLE IF NOT EXISTS + 已有資料就中止，重複執行不會產生副作用
  2. seed_customer_profiles() 用專屬亂數種子，不碰共用序列，
     所以就地新增與完整重跑會產生完全相同的資料
  3. 事後跑 verify_base_tables()，核心 4 張表的 sha256 一有變動就會叫

用法：
    python tools/add_customer_profiles.py --apply
    python tools/add_customer_profiles.py            # 只檢查，不寫入
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# init_db / init_db_ext 在 db/ 底下（2026-08-20 分類搬遷）
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db"))

from sqlalchemy import text  # noqa: E402

from init_db import verify_base_tables  # noqa: E402
from init_db_ext import EXT_DDL, seed_customer_profiles  # noqa: E402
from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

TABLE = "customer_profiles"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="實際寫入，否則只檢查")
    args = ap.parse_args()

    ddl = [d for d in EXT_DDL if f"CREATE TABLE IF NOT EXISTS {TABLE}" in d]
    if len(ddl) != 1:
        print(f"在 EXT_DDL 裡找不到唯一的 {TABLE} DDL（找到 {len(ddl)} 筆）")
        return 1

    engine = get_db_manager(MYSQL_URI).engine
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": TABLE}).scalar()
        rows = conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar() if exists else 0
        print(f"{TABLE}: {'已存在' if exists else '不存在'}，目前 {rows} 列")

        if not args.apply:
            print("（乾跑，未寫入。加 --apply 實際執行）")
            return 0
        if rows:
            print("已經有資料，不重複寫入。要重建請先手動 DROP TABLE。")
            return 0

        conn.execute(text(ddl[0]))
        n = seed_customer_profiles(conn)
        conn.commit()
        print(f"已寫入 {n} 列")

        cols = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": TABLE}).scalar()
        total = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()")).scalar()
        ntab = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()")).scalar()
        print(f"{TABLE} {cols} 欄；全庫 {ntab} 張表 / {total} 欄")

    print("\n核心表指紋檢查（145 題 GT 綁在這份資料上）：")
    verify_base_tables(engine)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
