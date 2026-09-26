"""配置加载模块。负责读取支持注释的 JSON/JSONC 配置，并向仿真与 API 入口提供标准 Python 字典。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Union


def get_runtime_root() -> Path:
    """返回当前配置和可写运行数据所在的根目录。

    Returns:
        源码运行时为本文件所在的项目根；PyInstaller 目录包运行时为
        ``hcd-api.exe`` 所在目录，从而让外置 ``config/`` 不落入 ``_internal``。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_runtime_path(path: Union[str, Path]) -> Path:
    """解析源码和 PyInstaller 目录包都可访问的运行文件路径。

    Args:
        path: 配置或运行数据的绝对路径、相对路径。

    Returns:
        Path: 已存在时优先返回外置运行目录中的路径；目录包未外置该文件时，
        回退到 PyInstaller ``_internal`` 数据目录。
    """
    requested = Path(path)
    if requested.is_absolute() or requested.exists():
        return requested

    # 部署包优先使用 hcd-api.exe 同级目录的可编辑 config/；源码环境则为项目根。
    runtime_candidate = get_runtime_root() / requested
    if runtime_candidate.exists():
        return runtime_candidate

    # PyInstaller onedir 模式将 --add-data 文件放在 _MEIPASS（即 _internal）中。
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundle_candidate = Path(bundle_root) / requested
        if bundle_candidate.exists():
            return bundle_candidate

    return requested


def _strip_json_comments(text: str) -> str:
    """移除 JSONC 中的注释。

    Args:
        text (str): 该方法的输入 json/jsonc 文本。

    Returns:
        str: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
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
    """执行 `loads_jsonc` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：text（str）
    输出：Any

    Args:
        text (str): 供当前处理流程使用的 `text` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    return json.loads(_strip_json_comments(text))


def load_jsonc(path: Union[str, Path]) -> Any:
    """加载配置、文件或既有状态，并转换为当前模块可消费的数据。

    输入：path（Union[str, Path]）
    输出：Any

    Args:
        path (Union[str, Path]): 需要读取、写入或检查的文件路径。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    return loads_jsonc(Path(path).read_text(encoding="utf-8"))
