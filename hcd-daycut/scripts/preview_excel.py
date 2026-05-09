from __future__ import annotations

import argparse

from xlsx_miniread import XlsxBook


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx_path")
    ap.add_argument("--rows", type=int, default=8)
    args = ap.parse_args()

    book = XlsxBook(args.xlsx_path)
    try:
        print("sheets:", book.sheet_names())
        for name in book.sheet_names():
            print()
            print("==", name, "==")
            rows = book.read_sheet_rows(name, max_rows=args.rows)
            for r in rows:
                print(r)
    finally:
        book.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
