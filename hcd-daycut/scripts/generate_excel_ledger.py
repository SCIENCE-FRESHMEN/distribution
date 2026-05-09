from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from xlsx_miniread import XlsxBook


def _find_header_row(rows: List[List[str]], key: str = "序号") -> int | None:
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


def extract_ledger(xlsx_path: str) -> List[Dict[str, Any]]:
    book = XlsxBook(xlsx_path)
    try:
        ledger: List[Dict[str, Any]] = []
        for sh in book.sheets():
            rows = book.read_sheet_rows(sh.name)
            hdr_i = _find_header_row(rows)
            if hdr_i is None:
                continue
            dicts = _rows_to_dicts(rows, hdr_i)
            if not dicts:
                continue

            for d in dicts:
                seq = (d.get("序号") or "").strip()
                api = (d.get("接口") or "").strip()
                okng = (d.get("OK/NG") or d.get("OK") or "").strip()
                closed = (d.get("关闭状态") or d.get("说明") or "").strip()
                issue = (d.get("问题点") or d.get("问题") or "").strip()
                typ = (d.get("类型") or "").strip()

                if not seq:
                    continue
                # Keep all meaningful rows, but drop pure placeholders.
                if not (api or okng or issue or typ or closed):
                    continue

                ledger.append(
                    {
                        "id": f"{sh.name}#{seq}",
                        "sheet": sh.name,
                        "seq": seq,
                        "type": typ,
                        "api": api,
                        "ok_ng": okng,
                        "closed": closed,
                        "issue": issue,
                    }
                )
        return ledger
    finally:
        book.close()


def to_markdown(items: List[Dict[str, Any]]) -> str:
    lines = []
    lines.append("# Excel Ledger")
    lines.append("")
    lines.append("来源: `库管系统算法升级接口测试记录_20260419.xlsx`")
    lines.append("")
    lines.append("| ID | Sheet | 序号 | 接口 | OK/NG | 关闭状态 | 问题点 |")
    lines.append("|---|---|---:|---|---|---|---|")
    for it in items:
        issue = (it.get("issue") or "").replace("\n", "<br>")
        lines.append(
            f"| {it['id']} | {it['sheet']} | {it['seq']} | {it.get('api','')} | {it.get('ok_ng','')} | {it.get('closed','')} | {issue} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx_path")
    ap.add_argument("--out-json", default="docs/excel_20260419_ledger.json")
    ap.add_argument("--out-md", default="docs/excel_20260419_ledger.md")
    args = ap.parse_args()

    items = extract_ledger(args.xlsx_path)

    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(to_markdown(items), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

