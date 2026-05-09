from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET


_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def _col_letters_to_index(col: str) -> int:
    col = col.upper()
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1  # 0-based


_CELL_REF_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _cell_ref_to_rc(ref: str) -> Tuple[int, int]:
    """
    Convert Excel cell ref like 'B2' into (row_idx, col_idx), both 0-based.
    """
    m = _CELL_REF_RE.match(ref)
    if not m:
        raise ValueError(f"Unsupported cell ref: {ref!r}")
    col_s, row_s = m.group(1), m.group(2)
    return int(row_s) - 1, _col_letters_to_index(col_s)


def _find_text(node: Optional[ET.Element]) -> str:
    if node is None or node.text is None:
        return ""
    return node.text


@dataclass(frozen=True)
class XlsxSheet:
    name: str
    path: str  # zip member path like 'xl/worksheets/sheet1.xml'


class XlsxBook:
    """
    Minimal XLSX reader without external dependencies.

    Supports:
    - shared strings (t="s")
    - inline strings (t="inlineStr")
    - plain values (numbers / t="str")

    Not supported (intentionally):
    - styles / date formatting
    - formulas (we return the cached <v>)
    - rich text beyond concatenating all <t> nodes
    """

    def __init__(self, xlsx_path: str):
        self.xlsx_path = xlsx_path
        self._zip = zipfile.ZipFile(xlsx_path)
        self._shared_strings = self._load_shared_strings()
        self._sheets = self._load_sheets()

    def close(self) -> None:
        self._zip.close()

    def sheet_names(self) -> List[str]:
        return [s.name for s in self._sheets]

    def sheets(self) -> List[XlsxSheet]:
        return list(self._sheets)

    def read_sheet_rows(self, sheet_name: str, *, max_rows: Optional[int] = None) -> List[List[str]]:
        sheet = next((s for s in self._sheets if s.name == sheet_name), None)
        if sheet is None:
            raise KeyError(f"Unknown sheet: {sheet_name!r}. Available: {self.sheet_names()}")
        xml = self._zip.read(sheet.path)
        root = ET.fromstring(xml)

        rows: Dict[int, Dict[int, str]] = {}
        max_col = -1
        for row in root.findall(".//main:sheetData/main:row", _NS):
            for c in row.findall("main:c", _NS):
                ref = c.attrib.get("r")
                if not ref:
                    continue
                r_i, c_i = _cell_ref_to_rc(ref)
                val = self._read_cell_value(c)
                if r_i not in rows:
                    rows[r_i] = {}
                rows[r_i][c_i] = val
                if c_i > max_col:
                    max_col = c_i

        if not rows:
            return []

        out: List[List[str]] = []
        max_r = max(rows.keys())
        for r_i in range(0, max_r + 1):
            if max_rows is not None and len(out) >= max_rows:
                break
            row_map = rows.get(r_i, {})
            out.append([row_map.get(c_i, "") for c_i in range(0, max_col + 1)])
        return out

    def _load_shared_strings(self) -> List[str]:
        try:
            xml = self._zip.read("xl/sharedStrings.xml")
        except KeyError:
            return []
        root = ET.fromstring(xml)
        out: List[str] = []
        for si in root.findall(".//main:si", _NS):
            # shared string can have rich text pieces <r><t>..</t></r>
            ts = [t.text or "" for t in si.findall(".//main:t", _NS)]
            out.append("".join(ts))
        return out

    def _load_sheets(self) -> List[XlsxSheet]:
        wb = ET.fromstring(self._zip.read("xl/workbook.xml"))

        rels = ET.fromstring(self._zip.read("xl/_rels/workbook.xml.rels"))
        id_to_target: Dict[str, str] = {}
        for rel in rels.findall(".//pkgrel:Relationship", _NS):
            rid = rel.attrib.get("Id")
            target = rel.attrib.get("Target")
            if rid and target:
                # Target is relative to xl/
                if target.startswith("/"):
                    # very rare in simple files
                    target_path = target.lstrip("/")
                else:
                    target_path = "xl/" + target.lstrip("./")
                id_to_target[rid] = target_path

        sheets: List[XlsxSheet] = []
        for sh in wb.findall(".//main:sheets/main:sheet", _NS):
            name = sh.attrib.get("name") or ""
            rid = sh.attrib.get(f"{{{_NS['rel']}}}id")
            if not name or not rid:
                continue
            target = id_to_target.get(rid)
            if not target:
                continue
            sheets.append(XlsxSheet(name=name, path=target))
        return sheets

    def _read_cell_value(self, c: ET.Element) -> str:
        t = c.attrib.get("t")
        if t == "s":
            v = _find_text(c.find("main:v", _NS))
            if not v:
                return ""
            try:
                idx = int(v)
            except ValueError:
                return v
            if 0 <= idx < len(self._shared_strings):
                return self._shared_strings[idx]
            return v

        if t == "inlineStr":
            ts = [t_el.text or "" for t_el in c.findall(".//main:is/main:t", _NS)]
            if not ts:
                # some files put <t> deeper
                ts = [t_el.text or "" for t_el in c.findall(".//main:t", _NS)]
            return "".join(ts)

        # numbers and cached formula results are stored in <v>
        v = _find_text(c.find("main:v", _NS))
        if v:
            return v

        # sometimes strings are in <is> without inlineStr
        ts = [t_el.text or "" for t_el in c.findall(".//main:t", _NS)]
        if ts:
            return "".join(ts)
        return ""


def read_xlsx_sheet_preview(xlsx_path: str, *, max_rows: int = 5) -> Dict[str, List[List[str]]]:
    book = XlsxBook(xlsx_path)
    try:
        out: Dict[str, List[List[str]]] = {}
        for sh in book.sheets():
            out[sh.name] = book.read_sheet_rows(sh.name, max_rows=max_rows)
        return out
    finally:
        book.close()

