"""
Schema 解析器
=============
從 semantic_layer.yaml 載入並解析：DDL、Enum、商業規則、Few-Shot 範例。
同時提供 Table/Column 白名單供 AST Validator 使用。
"""
import os
import re
from collections.abc import Iterable

import yaml
from loguru import logger as log


class SchemaParser:
    """解析 semantic_layer.yaml 中的所有資訊。"""

    def __init__(self, yaml_path: str | None = None):
        if yaml_path is None:
            # 預設路徑：d:\text_to_sql\utils\semantic_layer.yaml
            _project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            yaml_path = os.path.join(_project_root, "utils", "semantic_layer.yaml")

        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
            log.info(f"Semantic Layer 載入成功: {yaml_path}")
        except Exception as e:
            log.error(f"Semantic Layer 載入失敗: {e}")
            self._data = {}

        # 快取解析結果
        self._allowed_tables: list[str] | None = None
        self._table_columns: dict[str, list[str]] | None = None

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------
    def get_ddl(self) -> str:
        """取得完整 DDL 文字。"""
        return (self._data.get("ddl") or "").strip()

    def get_ddl_for(self, tables: Iterable[str] | None) -> str:
        """
        只取指定那幾張表的 DDL。tables 為 None 時回傳完整 DDL。

        DDL 是由 tools/gen_ddl.py 從 INFORMATION_SCHEMA 產生的，格式固定為
        以空行分隔的 CREATE TABLE 區塊，所以可以安全地照區塊切。
        找不到任何一張表時退回完整 DDL —— 給模型一份空的 schema，
        會讓它開始憑空捏造欄位，那比 Prompt 長要糟得多。
        """
        full = self.get_ddl()
        if tables is None:
            return full

        wanted = {t.lower() for t in tables}
        blocks = [b for b in full.split("\n\n") if b.strip()]
        picked = []
        for block in blocks:
            match = re.match(r"\s*CREATE TABLE\s+(\w+)", block, re.I)
            if match and match.group(1).lower() in wanted:
                picked.append(block.strip())

        if not picked:
            log.warning(f"[Schema] 這些表在 DDL 裡一張都找不到: {sorted(wanted)}，"
                        "退回完整 DDL")
            return full
        return "\n\n".join(picked)

    # ------------------------------------------------------------------
    # Enum 欄位說明
    # ------------------------------------------------------------------
    def get_enum_text(self, tables: Iterable[str] | None = None) -> str:
        """
        取得 Enum 欄位的格式化說明文字。

        給了 tables 就只列這些表的 enum —— 剪掉了 DDL 卻留著全部的 enum，
        等於在告訴模型有一些它看不到 schema 的欄位可以用。
        """
        wanted = None if tables is None else {t.lower() for t in tables}
        lines: list[str] = []
        for field, info in (self._data.get("enum_fields") or {}).items():
            if wanted is not None and field.split(".")[0].lower() not in wanted:
                continue
            lines.append(f"  {field} ({info.get('description', '')})")
            for val, desc in (info.get("values") or {}).items():
                lines.append(f"    - '{val}' = {desc}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 商業邏輯規則
    # ------------------------------------------------------------------
    def get_rules_text(self) -> str:
        """取得商業規則的格式化說明文字。"""
        lines: list[str] = []
        for rule in self._data.get("business_rules") or []:
            lines.append(f"  【{rule['name']}】")
            # description 說明「為什麼」，是模型判斷規則適用與否的依據；
            # 過去只送 correct/wrong SQL，等於要模型從兩段程式碼反推語意。
            desc = (rule.get("description") or "").strip()
            if desc:
                indented = "\n".join(
                    f"    {line}" for line in desc.splitlines() if line.strip()
                )
                lines.append(indented)
            lines.append(f"    ✅ 正確：{rule['correct_sql']}")
            lines.append(f"    ❌ 錯誤：{rule['wrong_sql']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Few-Shot 範例
    # ------------------------------------------------------------------
    def get_few_shot_text(self) -> str:
        """取得 Few-Shot 範例的格式化文字。"""
        lines: list[str] = []
        for ex in self._data.get("few_shot_examples") or []:
            lines.append(f"  Question: {ex['question']}")
            # reasoning 說明「為什麼是這個寫法」。這裡曾經只送 question + SQL ——
            # 等於要模型從一份答案反推它示範的是哪一條教訓，口徑選擇、粒度、
            # 量詞這些教訓級的內容根本傳不過去。get_rules_text() 早就為
            # business_rules 修過同一個問題（見上），這裡漏了：YAML 裡
            # 每一則 reasoning 都寫了，卻從來沒有進過 Prompt。
            reasoning = (ex.get("reasoning") or "").strip()
            if reasoning:
                lines.append("\n".join(
                    f"      {ln}" for ln in reasoning.splitlines() if ln.strip()))
            if "sql_step1" in ex and "sql_step2" in ex:
                s1 = ex["sql_step1"].strip().replace("\n", "\n          ")
                s2 = ex["sql_step2"].strip().replace("\n", "\n          ")
                lines.append(f"  SQL (Step 1):\n          {s1}")
                lines.append(f"  SQL (Step 2):\n          {s2}")
            else:
                sql_block = ex["sql"].strip().replace("\n", "\n          ")
                lines.append(f"  SQL:\n          {sql_block}")
            # 反例：只給正解，模型分不出「這樣寫也對」與「這樣寫是錯的」。
            wrong = (ex.get("wrong_sql") or "").strip()
            if wrong:
                lines.append("  ❌ 錯誤寫法:\n          "
                             + wrong.replace("\n", "\n          "))
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 從 DDL 提取合法 Table 清單
    # ------------------------------------------------------------------
    def get_allowed_tables(self) -> list[str]:
        """從 DDL 中解析出所有 Table 名稱。"""
        if self._allowed_tables is not None:
            return self._allowed_tables
        ddl = self.get_ddl()
        self._allowed_tables = re.findall(
            r"CREATE\s+TABLE\s+(\w+)\s*\(", ddl, re.IGNORECASE
        )
        return self._allowed_tables

    # ------------------------------------------------------------------
    # 從 DDL 提取 Table → Columns 映射
    # ------------------------------------------------------------------
    def get_table_columns(self) -> dict[str, list[str]]:
        """從 DDL 中解析出 {table_name: [column_names]} 映射。"""
        if self._table_columns is not None:
            return self._table_columns

        ddl = self.get_ddl()
        self._table_columns = {}

        # 用 regex 拆出每個 CREATE TABLE 區塊
        # `\)` 後面要容許表級 COMMENT —— gen_ddl.py 從 2026-08-19 起會輸出
        # `) COMMENT '...';`（§10.7）。原本的 `\);` 一個區塊都對不上，
        # 這支會靜默回傳空 dict：它是 schema_registry 連不上 MySQL 時的
        # 降級來源，也是 _report_drift 的比對基準，壞掉不會有人喊。
        pattern = r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\)\s*(?:COMMENT\s*'(?:[^']|'')*')?\s*;"
        for match in re.finditer(pattern, ddl, re.IGNORECASE | re.DOTALL):
            table_name = match.group(1).lower()
            body = match.group(2)
            columns: list[str] = []

            for line in body.split("\n"):
                stripped = line.strip()
                if not stripped:
                    continue
                # 跳過非欄位定義的行（FOREIGN KEY, PRIMARY KEY 等）
                first_word = stripped.split()[0].upper().rstrip(",")
                if first_word in (
                    "FOREIGN", "PRIMARY", "KEY", "INDEX", "UNIQUE",
                    "CONSTRAINT", "CHECK", ")",
                ):
                    continue
                col_name = stripped.split()[0].strip(",").lower()
                if col_name:
                    columns.append(col_name)

            self._table_columns[table_name] = columns

        return self._table_columns


# ===========================================================================
# 全域單例
# ===========================================================================
_parser: SchemaParser | None = None


def get_schema_parser() -> SchemaParser:
    """取得全域 SchemaParser 單例。"""
    global _parser
    if _parser is None:
        _parser = SchemaParser()
    return _parser
