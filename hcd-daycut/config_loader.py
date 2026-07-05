from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Union


def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments while preserving string literals."""
    result = []
    i = 0
    n = len(text)
    in_string = False
    string_quote = ""
    escape = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_string:
            result.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == string_quote:
                in_string = False
            i += 1
            continue

        if ch in {'"', "'"}:
            in_string = True
            string_quote = ch
            result.append(ch)
            i += 1
            continue

        if ch == "/" and nxt == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue

        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2 if i + 1 < n else 0
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def loads_jsonc(text: str) -> Any:
    return json.loads(_strip_json_comments(text))


def load_jsonc(path: Union[str, Path]) -> Any:
    return loads_jsonc(Path(path).read_text(encoding="utf-8"))
