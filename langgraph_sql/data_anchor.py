"""
資料時間錨點與亂數種子
========================
seed 出來的假資料以 DATA_ANCHOR_DATE 當作「今天」往回推。把它固定下來
（而不是用 datetime.now()）的理由有兩個，都是為了讓 ground truth 能進版控：

  1. 原本每次重新 seed，全部日期就整體平移、亂數也重骰一次，
     任何預期答案立刻失效 —— 等於每次驗證都要從頭手算一遍。
  2. 題庫裡同時存在絕對日期題（「2026 年 5 月的總營收」）與相對時間題
     （「最近 3 個月註冊的客戶」）。只要資料跟著真實時鐘走，資料窗口就會
     每天前移，絕對日期題遲早落到資料範圍之外，靜默變成 0 筆。

因此「資料的今天」與「模型認知的今天」一起固定成同一個日期。這個日期會
注入 Prompt，讓模型拿它去解析相對時間而不是用 CURDATE()。生產環境的
Text-to-SQL 本來就該這樣做：一句查詢的語意不應該取決於它在哪一天被執行。

兩個值都可用環境變數覆寫，但覆寫等於換一份資料，既有的預期答案要重算。
"""
import os
from datetime import date, datetime

# 資料集的「今天」。所有 seed 出來的日期都是從這裡往回推算。
DATA_ANCHOR_DATE: date = date.fromisoformat(
    os.getenv("DATA_ANCHOR_DATE", "2026-08-15")
)

# 截到午夜，讓同一天內重複執行 init_db.py 產生完全一致的資料。
DATA_ANCHOR_DATETIME: datetime = datetime.combine(
    DATA_ANCHOR_DATE, datetime.min.time()
)

# 假資料的亂數種子。固定後，客戶姓名、商品庫存、訂單組合、金額全部可重現。
DATA_RANDOM_SEED: int = int(os.getenv("DATA_RANDOM_SEED", "20260815"))
