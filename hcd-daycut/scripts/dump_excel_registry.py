from __future__ import annotations

import argparse
import json

from extract_excel_issues import main as _unused  # noqa: F401
from xlsx_miniread import XlsxBook


def _find_header_row(rows, key: str = "序号"):
    for i, r in enumerate(rows):
        if any((c or "").strip() == key for c in r):
            return i
    return None


def _rows_to_dicts(rows, header_idx: int):
    header = [(c or "").strip() for c in rows[header_idx]]
    out = []
    for r in rows[header_idx + 1 :]:
        if not any((c or "").strip() for c in r):
            continue
        d = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        out.append(d)
    return out


def dump_registry(xlsx_path: str) -> list[dict]:
    book = XlsxBook(xlsx_path)
    try:
        all_rows = []
        for sh in book.sheets():
            rows = book.read_sheet_rows(sh.name)
            hdr_i = _find_header_row(rows)
            if hdr_i is None:
                continue
            dicts = _rows_to_dicts(rows, hdr_i)
            if not dicts:
                continue

            ok_ng_key = next((k for k in dicts[0].keys() if "OK" in k or "OK/NG" in k), None)
            close_key = next((k for k in dicts[0].keys() if "关闭" in k), None)
            issue_key = next((k for k in dicts[0].keys() if "问题" in k), None)

            for d in dicts:
                all_rows.append(
                    {
                        "sheet": sh.name,
                        "seq": (d.get("序号") or "").strip(),
                        "type": (d.get("类型") or "").strip(),
                        "api": (d.get("接口") or "").strip(),
                        "ok_ng": (d.get(ok_ng_key) or "").strip() if ok_ng_key else "",
                        "closed": (d.get(close_key) or "").strip() if close_key else "",
                        "issue": (d.get(issue_key) or "").strip() if issue_key else "",
                        "raw": d,
                    }
                )
        return all_rows
    finally:
        book.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx_path")
    ap.add_argument("--out", default="docs/excel_20260419_registry.json")
    args = ap.parse_args()

    data = dump_registry(args.xlsx_path)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(args.out)
    print("rows", len(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

