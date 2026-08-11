"""
Schema 解析器
=============
從 semantic_layer.yaml 載入並解析：DDL、Enum、商業規則、Few-Shot 範例。
同時提供 Table/Column 白名單供 AST Validator 使用。
"""
import os
import re
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

    # ------------------------------------------------------------------
    # Enum 欄位說明
    # ------------------------------------------------------------------
    def get_enum_text(self) -> str:
        """取得 Enum 欄位的格式化說明文字。"""
        lines: list[str] = []
        for field, info in (self._data.get("enum_fields") or {}).items():
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
            if "sql_step1" in ex and "sql_step2" in ex:
                s1 = ex["sql_step1"].strip().replace("\n", "\n          ")
                s2 = ex["sql_step2"].strip().replace("\n", "\n          ")
                lines.append(f"  SQL (Step 1):\n          {s1}")
                lines.append(f"  SQL (Step 2):\n          {s2}")
            else:
                sql_block = ex["sql"].strip().replace("\n", "\n          ")
                lines.append(f"  SQL:\n          {sql_block}")
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
        pattern = r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\);"
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
