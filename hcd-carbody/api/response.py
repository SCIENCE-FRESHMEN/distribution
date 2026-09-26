"""统一构造 API 成功和失败响应。"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


# ==========================================================================
# 主函数：统一 API 成功与失败响应构造
# ==========================================================================
def ok(
    data: Any = None,
    message: str = "success",
    http_status: int = 200,
    status_code: Any = "SUCCESS",
    code: Any | None = None,
) -> JSONResponse:
    """执行 ok 对应的业务处理。

    Args:
        data: 待转换或处理的数据。
        message: 用于本函数处理的 `message` 参数。
        http_status: 用于本函数处理的 `http_status` 参数。
        status_code: 用于本函数处理的 `status_code` 参数。
        code: 用于本函数处理的 `code` 参数。

    Returns:
        JSONResponse: 处理后的结果。
    """
    status_val = status_code if code is None else code
    return JSONResponse(
        status_code=http_status,
        content={
            "status": status_val,
            "message": str(message),
            "data": data,
        },
    )


def fail(
    message: str,
    http_status: int = 500,
    data: Any = None,
    status_code: Any = "FAILED",
    code: Any | None = None,
) -> JSONResponse:
    """执行 fail 对应的业务处理。

    Args:
        message: 用于本函数处理的 `message` 参数。
        http_status: 用于本函数处理的 `http_status` 参数。
        data: 待转换或处理的数据。
        status_code: 用于本函数处理的 `status_code` 参数。
        code: 用于本函数处理的 `code` 参数。

    Returns:
        JSONResponse: 处理后的结果。
    """
    status_val = status_code if code is None else code
    return JSONResponse(
        status_code=http_status,
        content={
            "status": status_val,
            "message": str(message),
            "data": data,
        },
    )
