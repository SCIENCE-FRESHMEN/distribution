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
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    book = XlsxBook(args.xlsx_path)
    try:
        all_issues = []
        for sh in book.sheets():
            rows = book.read_sheet_rows(sh.name, max_rows=args.max_rows)
            hdr_i = _find_header_row(rows)
            if hdr_i is None:
                continue
            dicts = _rows_to_dicts(rows, hdr_i)
            # heuristic fields
            ok_ng_key = next((k for k in dicts[0].keys() if "OK" in k or "OK/NG" in k), None) if dicts else None
            close_key = next((k for k in dicts[0].keys() if "关闭" in k), None) if dicts else None
            issue_key = next((k for k in dicts[0].keys() if "问题" in k), None) if dicts else None
            if not ok_ng_key:
                continue

            for d in dicts:
                okng = (d.get(ok_ng_key) or "").strip()
                close = (d.get(close_key) or "").strip() if close_key else ""
                issue = (d.get(issue_key) or "").strip() if issue_key else ""
                all_issues.append(
                    {
                        "sheet": sh.name,
                        "seq": (d.get("序号") or "").strip(),
                        "type": (d.get("类型") or "").strip(),
                        "api": (d.get("接口") or "").strip(),
                        "ok_ng": okng,
                        "closed": close,
                        "issue": issue,
                    }
                )

        if args.json:
            print(json.dumps(all_issues, ensure_ascii=False, indent=2))
        else:
            for it in all_issues:
                print(f"[{it['sheet']}] #{it['seq']} {it['api']} closed={it['closed']!r}")
                if it["issue"]:
                    print(it["issue"])
                print()
        return 0
    finally:
        book.close()


if __name__ == "__main__":
    raise SystemExit(main())
