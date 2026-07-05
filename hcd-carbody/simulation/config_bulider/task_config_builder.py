import json
import re
from pathlib import Path
from typing import Optional, List

import pandas as pd
import pytz


class TaskConfigBuilder:
    PRODUCTION_CODE_TO_LINE = {
        "WB1": 1,
        "PB1": 2,
        "WB2": 3,
        "PB3": 4,
    }

    PRODUCTION_CODE_TO_OUT_PORT = {
        "WB1": "L1C17",
        "PB1": "L1C1",
        "WB2": "L2C1",
        "PB3": "L3C17",
    }

    BLOCKED_INBOUND_SKUS = {
        "00000000000000",   # 14 zeros
        "000000000000000",  # 15 zeros
        "100000000000000",
    }

    def __init__(
        self,
        outbound_source_path: str = "simulation/data/outbound_task.xlsx",
        inbound_source_path: str = "simulation/data/inbound_task.xlsx",
        include_blocked_inbound_skus: bool = False,
    ):
        self.outbound_source_path = outbound_source_path
        self.inbound_source_path = inbound_source_path
        self.include_blocked_inbound_skus = bool(include_blocked_inbound_skus)

        self.inbound_records = []
        self.outbound_plan = {}
        self.outbound_creation_times = {}
        self.outbound_out_lines = {}
        self._skipped_rows = 0

    @staticmethod
    def _to_seconds(val) -> Optional[float]:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        try:
            if isinstance(val, pd.Timestamp):
                if val.tz is None:
                    local_tz = pytz.timezone("Asia/Shanghai")
                    return local_tz.localize(val).timestamp()
                return val.timestamp()
            if isinstance(val, (int, float)):
                return float(val)
            dt = pd.to_datetime(val)
            if dt.tz is None:
                local_tz = pytz.timezone("Asia/Shanghai")
                return local_tz.localize(dt).timestamp()
            return dt.timestamp()
        except Exception:
            return None

    @staticmethod
    def _to_str(val) -> Optional[str]:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        text = str(val).strip()
        return text if text else None

    @staticmethod
    def _parse_int_like(val) -> Optional[int]:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        try:
            if isinstance(val, (int, float)):
                return int(val)
            text = str(val).strip()
            if not text:
                return None
            if text.isdigit():
                return int(text)
            m = re.search(r"(\d+)", text)
            if m:
                return int(m.group(1))
        except Exception:
            return None
        return None

    @staticmethod
    def _normalize_port_key(val, default_col: int = 1, fallback: str = "L1C1") -> str:
        text = str(val).strip() if val is not None else ""
        if not text:
            return fallback
        text_u = text.upper().replace(" ", "")
        m = re.match(r"^L(\d+)C(\d+)$", text_u)
        if m:
            return f"L{int(m.group(1))}C{int(m.group(2))}"
        nums = re.findall(r"(\d+)", text)
        if len(nums) >= 2:
            return f"L{int(nums[0])}C{int(nums[1])}"
        if len(nums) == 1:
            return f"L{int(nums[0])}C{int(default_col)}"
        return fallback

    @staticmethod
    def _read_table(path: str) -> pd.DataFrame:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Input file not found: {path}")
        if p.suffix.lower() in {".xlsx", ".xls"}:
            try:
                return pd.read_excel(p)
            except Exception:
                return pd.read_csv(p)
        return pd.read_csv(p)

    @staticmethod
    def _find_col_exact(df: pd.DataFrame, names: List[str]) -> Optional[str]:
        for n in names:
            if n in df.columns:
                return n
        return None

    @staticmethod
    def _find_col_contains(df: pd.DataFrame, keywords: List[str]) -> Optional[str]:
        lower_map = {str(c).lower(): c for c in df.columns}
        for k in keywords:
            lk = k.lower()
            for cand_lower, raw in lower_map.items():
                if lk in cand_lower:
                    return raw
        return None

    @classmethod
    def _is_blocked_inbound_sku(cls, sku: str) -> bool:
        s = str(sku).strip()
        if not s:
            return True
        if s in cls.BLOCKED_INBOUND_SKUS:
            return True
        if s.isdigit():
            try:
                n = int(s)
                if n == 0 or n == 100000000000000:
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _build_outbound_sku10(car_body_id: str, skid_state_val: Optional[str]) -> str:
        body = str(car_body_id).strip()
        body_tail9 = body[-9:] if len(body) > 9 else body
        if len(body_tail9) < 9:
            body_tail9 = body_tail9.zfill(9)

        skid = str(skid_state_val).strip() if skid_state_val is not None else ""
        if skid:
            m = re.search(r"(\d)", skid)
            skid_digit = m.group(1) if m else skid[0]
        else:
            skid_digit = "0"
        return f"{skid_digit}{body_tail9}"

    def _collect_skus(
        self,
        row,
        car_body_col: Optional[str],
        skid_state_col: Optional[str],
        skid_type_col: Optional[str],
        color_col: Optional[str],
        role: str,
    ) -> List[dict]:
        if not car_body_col:
            return []

        skus = []
        color_val = self._to_str(row.get(color_col)) if color_col else None
        raw_sku = self._to_str(row.get(car_body_col))
        if not raw_sku:
            return []

        if role == "inbound":
            if not self.include_blocked_inbound_skus and self._is_blocked_inbound_sku(raw_sku):
                return []
            sku = raw_sku[-10:] if len(raw_sku) > 10 else raw_sku
        else:
            skid_state_val = self._to_str(row.get(skid_state_col)) if skid_state_col else None
            sku = self._build_outbound_sku10(raw_sku, skid_state_val)

        features = {}
        if color_val:
            features["color"] = color_val
        skid_type_val = self._to_str(row.get(skid_type_col)) if skid_type_col else None
        # Inbound fallback: infer skid_type from the first digit of RFID/car body id.
        # Example: "0..." -> short skid, "1..." -> long skid.
        if skid_type_val is None and role == "inbound":
            raw_text = str(raw_sku).strip()
            if raw_text:
                if raw_text[0].isdigit():
                    skid_type_val = raw_text[0]
                else:
                    m = re.search(r"(\d)", raw_text)
                    if m:
                        skid_type_val = m.group(1)
        if skid_type_val is not None:
            # normalized as string, e.g. "0" (short), "1" (long)
            features["skid_type"] = skid_type_val
        skus.append({"skuId": sku, "features": features})
        return skus

    def _resolve_columns(self, df: pd.DataFrame) -> dict:
        # use unicode escapes to avoid source-encoding issues
        col_type = self._find_col_exact(df, ["type", "task_type", "\u7c7b\u578b"])
        col_car_body = self._find_col_exact(df, ["\u8f66\u8eab\u7f16\u53f7", "RFID", "rfid"])
        if col_car_body is None:
            col_car_body = self._find_col_contains(df, ["\u8f66\u8eab\u7f16\u53f7", "rfid", "\u7f16\u53f7"])

        col_code = self._find_col_exact(df, ["\u006d\u0065\u0073\u8ba1\u5212\u7c7b\u578b", "mes_plan_type", "mes_type", "plan_type"])
        col_in_line = self._find_col_exact(df, ["in_line", "layer", "floor", "\u6240\u5c5e\u5c42"])
        col_out_line = self._find_col_exact(df, ["out_line", "outbound_line"])
        col_prod_line = self._find_col_exact(df, ["production_line", "line"])

        col_create = self._find_col_exact(df, ["\u521b\u5efa\u65f6\u95f4", "creation_time", "create_time"])
        col_start = self._find_col_exact(df, ["\u5f00\u59cb\u65f6\u95f4", "start_time"])
        col_color = self._find_col_exact(df, ["\u989c\u8272", "color"])
        col_skid_state = self._find_col_exact(df, ["\u6ed1\u6a47\u72b6\u6001", "skid_state"])  # 滑橇状态
        col_skid_type = self._find_col_exact(df, ["\u6ed1\u6a47\u7c7b\u578b", "skid_type", "skidType"]) 

        return {
            "type": col_type,
            "car_body": col_car_body,
            "code": col_code,
            "in_line": col_in_line,
            "out_line": col_out_line,
            "prod_line": col_prod_line,
            "create_time": col_create,
            "start_time": col_start,
            "color": col_color,
            "skid_state": col_skid_state,
            "skid_type": col_skid_type,
        }

    def _process_df(self, df: pd.DataFrame, forced_role: str, sort_by: Optional[str] = None):
        if sort_by and sort_by in df.columns:
            df = df.sort_values([sort_by])

        cols = self._resolve_columns(df)

        for _, row in df.iterrows():
            role = forced_role
            production_code = self._to_str(row.get(cols["code"]))
            if production_code:
                production_code = production_code.upper()

            mapped_pl = self.PRODUCTION_CODE_TO_LINE.get(production_code) if production_code else None
            fallback_pl = self._parse_int_like(row.get(cols["prod_line"]))
            production_line = mapped_pl if mapped_pl is not None else (fallback_pl if fallback_pl is not None else 1)

            raw_in_line = row.get(cols["in_line"]) if cols["in_line"] else None
            in_line = self._normalize_port_key(raw_in_line, default_col=1, fallback="L1C1")

            raw_out_line = row.get(cols["out_line"]) if cols["out_line"] else None
            if raw_out_line is None:
                raw_out_line = self.PRODUCTION_CODE_TO_OUT_PORT.get(production_code) if production_code else None
            if raw_out_line is None:
                raw_out_line = in_line
            out_line = self._normalize_port_key(raw_out_line, default_col=17, fallback=in_line)

            skus = self._collect_skus(
                row,
                cols["car_body"],
                cols["skid_state"],
                cols["skid_type"],
                cols["color"],
                role=role,
            )
            if not skus:
                self._skipped_rows += 1
                continue

            create_time = self._to_seconds(row.get(cols["create_time"])) if cols["create_time"] else None
            start_time = self._to_seconds(row.get(cols["start_time"])) if cols["start_time"] else None
            task_time = start_time if start_time is not None else create_time

            if role == "inbound":
                self.inbound_records.append(
                    {
                        "arrival_time": task_time,
                        "skus": skus,
                        "in_line": in_line,
                        "production_line": int(production_line),
                        "out_line": out_line,
                    }
                )
            else:
                line_key = str(int(production_line))
                self.outbound_plan.setdefault(line_key, [])
                self.outbound_creation_times.setdefault(line_key, [])
                self.outbound_out_lines.setdefault(line_key, [])
                self.outbound_plan[line_key].append([skus])
                self.outbound_creation_times[line_key].append(task_time)
                self.outbound_out_lines[line_key].append(out_line)

    def build(self, sort_by: Optional[str] = None):
        self.inbound_records = []
        self.outbound_plan = {}
        self.outbound_creation_times = {}
        self.outbound_out_lines = {}
        self._skipped_rows = 0

        outbound_df = self._read_table(self.outbound_source_path)
        inbound_df = self._read_table(self.inbound_source_path)
        self._process_df(outbound_df, forced_role="outbound", sort_by=sort_by)
        self._process_df(inbound_df, forced_role="inbound", sort_by=sort_by)
        return self

    def to_dict(self):
        return {
            "inbound_records": self.inbound_records,
            "production_plan": self.outbound_plan,
            "creation_times": self.outbound_creation_times,
            "out_lines": self.outbound_out_lines,
        }

    def save_split_json(
        self,
        inbound_path: str = "simulation/data/inbound_task_config.json",
        outbound_path: str = "simulation/data/outbound_task_config.json",
    ):
        Path(inbound_path).parent.mkdir(parents=True, exist_ok=True)
        Path(outbound_path).parent.mkdir(parents=True, exist_ok=True)

        with open(inbound_path, "w", encoding="utf-8") as f:
            json.dump({"inbound_records": self.inbound_records}, f, ensure_ascii=False, indent=4)

        with open(outbound_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "production_plan": self.outbound_plan,
                    "creation_times": self.outbound_creation_times,
                    "out_lines": self.outbound_out_lines,
                },
                f,
                ensure_ascii=False,
                indent=4,
            )

        print(f"Inbound config saved to: {inbound_path}")
        print(f"Outbound config saved to: {outbound_path}")
        if self._skipped_rows:
            print(f"Skipped rows (missing/filtered SKU): {self._skipped_rows}")


if __name__ == "__main__":
    builder = TaskConfigBuilder().build()
    builder.save_split_json(
        "simulation/data/inbound_task_config.json",
        "simulation/data/outbound_task_config.json",
    )
