from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def ok(
    data: Any = None,
    message: str = "success",
    http_status: int = 200,
    status_code: Any = "SUCCESS",
    code: Any | None = None,
) -> JSONResponse:
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
    status_val = status_code if code is None else code
    return JSONResponse(
        status_code=http_status,
        content={
            "status": status_val,
            "message": str(message),
            "data": data,
        },
    )
