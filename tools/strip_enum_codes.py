# -*- coding: utf-8 -*-
"""把欄位註解裡的代碼字面拿掉，中文說明留下 —— E14／E15 共用的同一份實作。

兩種寫法要分開處理：

    括號列舉   投放渠道 (EMAIL/SOCIAL/SEARCH/DISPLAY)   → 投放渠道
               整組括號拿掉。括號裡沒有中文，沒有東西可留。

    冒號列舉   投放版位：FEED 動態／STORY 限動／…        → 投放版位：動態／限動／…
               只拿掉代碼，中文註釋留著 —— 那正是「要中文說明」的部分。

**資格看活值，動手拿全部。** 一欄要合格，得在註解裡提到 >= 2 個代碼、而且
至少一個是資料裡真的有的值 —— 這樣「與即時 SUM(...) 可能不同」這種散文不會
被誤判成列舉。合格之後拿掉的是**註解裡全部的代碼 token**，包含死代碼：
`shipments.status` 註解列了 PREPARING/IN_TRANSIT/DELIVERED/RETURNED 而資料裡
只有中間兩個，只拿活的會留下「(PREPARING///RETURNED)」。死代碼在 `enums`
裡宣告過（閘門 [4b] 正在數那 12 個），所以生成器不會失血。

散文內嵌（「非品質問題為 NONE」）拿掉就不成句，靠 >= 2 這條擋掉。
`payments.status` 的 FAILED／REFUNDED 是 `#104`（expect: empty）的前提，
`customer_profiles.risk_flag` 的 HIGH／WATCH 是 `#159` 的前提，走 FORBIDDEN。
"""
import re

# 這兩欄的代碼是 `#104`／`#159` 兩題 `expect: empty` 的前提：
# GT note 寫「考的是欄位註解列了某個值 ≠ 資料裡真的有」。清掉題目就沒了。
FORBIDDEN = {("payments", "status"), ("customer_profiles", "risk_flag")}

# 判資格用：長度 >= 3，避免把 `M`/`F`/`AD` 這種當成列舉的證據。
_CODE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
# 動手拿用：括號內連 `AD`、`M` 這種兩字以下的代碼也要一起帶走，
# 否則會留下「流量來源 (///AD)」。括號外不用這條 —— 太寬會傷到中文句子。
_ANY = re.compile(r"\b[A-Z][A-Z0-9_]*\b")
_SEP = "／/、,，"
_PAREN = re.compile(r"\s*[（(]\s*(?:[A-Z][A-Z0-9_]*\s*[%s]?\s*)+[）)]" % _SEP)


def codes_in(comment: str, values) -> set[str]:
    """註解裡出現、而且確實是這個欄位**資料值**的代碼（判資格用）。"""
    return set(_CODE.findall(comment or "")) & {v for v in values if _CODE.fullmatch(v)}


def should_strip(table: str, column: str, comment: str, values) -> bool:
    """>= 2 個代碼 ＋ 至少一個是活值 = 值列舉；否則是散文內嵌，不動。"""
    if (table, column) in FORBIDDEN:
        return False
    return len(_CODE.findall(comment or "")) >= 2 and bool(codes_in(comment, values))


def strip(comment: str, values=()) -> str:
    """拿掉註解裡全部的代碼 token，收尾成通順的中文。"""
    if not comment:
        return comment
    out = _PAREN.sub("", comment)            # ① 整組括號都是代碼 → 連括號拿掉
    out = _CODE.sub("", out)                 # ② 其餘位置拿掉代碼字面
    out = re.sub(r"(?<=[%s：:])[ \u3000]+" % _SEP, "", out)   # 分隔符後的殘留空白
    out = re.sub(r"[%s]{2,}" % _SEP, "／", out)               # 連續分隔（該值沒有中文註釋）
    out = re.sub(r"[（(][\s%s]*[）)]" % _SEP, "", out)         # 空括號
    out = re.sub(r"(?<=[：:])[%s]" % _SEP, "", out)           # 冒號後緊接分隔
    out = re.sub(r"[%s](?=[）)。，,；;]|$)" % _SEP, "", out)   # 句尾懸空分隔
    return re.sub(r"[ \u3000]{2,}", " ", out).strip(" 　：:，," + _SEP)
