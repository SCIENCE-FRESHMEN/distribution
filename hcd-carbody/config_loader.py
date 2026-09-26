"""加载带注释的运行配置，并兼容源码目录与 PyInstaller 目录包。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Union


def get_runtime_root() -> Path:
    """返回可编辑运行配置所在的根目录。

    Returns:
        Path: 源码模式下为项目根目录；PyInstaller 目录包模式下为 exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_runtime_path(path: Union[str, Path]) -> Path:
    """解析源码与目录包均可使用的配置文件路径。

    外置 ``config/`` 优先于 PyInstaller ``_internal`` 内置副本，因此部署后修改
    exe 同级配置即可在重启服务时生效。

    Args:
        path: 绝对路径或相对项目路径。

    Returns:
        Path: 已找到的外置或内置路径；均未找到时返回原始相对路径。
    """
    requested = Path(path)
    if requested.is_absolute() or requested.exists():
        return requested

    runtime_candidate = get_runtime_root() / requested
    if runtime_candidate.exists():
        return runtime_candidate

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundle_candidate = Path(bundle_root) / requested
        if bundle_candidate.exists():
            return bundle_candidate

    return requested


def _strip_json_comments(text: str) -> str:
    """移除 JSONC 的行注释和块注释，同时保留字符串内的 ``//``。

    Args:
        text: 原始 JSONC 文本。

    Returns:
        str: 可交给标准 ``json.loads`` 解析的 JSON 文本。
    """
    result: list[str] = []
    index = 0
    in_string = False
    quote = ""
    escaped = False

    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            quote = char
            result.append(char)
            index += 1
            continue

        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue

        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2 if index + 1 < len(text) else 0
            continue

        result.append(char)
        index += 1

    return "".join(result)


def load_jsonc(path: Union[str, Path]) -> Any:
    """读取并解析 JSON 或 JSONC 配置。

    Args:
        path: 需要读取的配置文件路径。

    Returns:
        Any: JSON 顶层对象；语法错误由调用方处理并决定是否回退。
    """
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(_strip_json_comments(text))
