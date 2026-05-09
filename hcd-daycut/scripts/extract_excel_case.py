from __future__ import annotations

import argparse
import json
from typing import Dict, List, Optional

from xlsx_miniread import XlsxBook


def _find_header_row(rows: List[List[str]], key: str = "序号") -> Optional[int]:
    for i, r in enumerate(rows):
        if any((c or "").strip() == key for c in r):
            return i
    return None


def _rows_to_dicts(rows: List[List[str]], header_idx: int) -> List[Dict[str, str]]:
    header = [(c or "").strip() for c in rows[header_idx]]
    out: List[Dict[str, str]] = []
    for r in rows[header_idx + 1 :]:
        if not any((c or "").strip() for c in r):
            continue
        d = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx_path")
    ap.add_argument("--sheet", required=True)
    ap.add_argument("--seq", required=True)
    args = ap.parse_args()

    book = XlsxBook(args.xlsx_path)
    try:
        rows = book.read_sheet_rows(args.sheet)
        hdr_i = _find_header_row(rows)
        if hdr_i is None:
            raise SystemExit(f"Header row not found in sheet {args.sheet!r}")
        dicts = _rows_to_dicts(rows, hdr_i)
        hit = next((d for d in dicts if (d.get("序号") or "").strip() == args.seq), None)
        if not hit:
            raise SystemExit(f"Seq {args.seq!r} not found in sheet {args.sheet!r}")
        print(json.dumps(hit, ensure_ascii=False, indent=2))
        return 0
    finally:
        book.close()


if __name__ == "__main__":
    raise SystemExit(main())

