from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details


def request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value or uuid.uuid4())


def error_body(
    request: Request,
    code: str,
    message: str,
    retryable: bool,
    details: Any = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": details,
        "request_id": request_id(request),
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(request, exc.code, exc.message, exc.retryable, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(
                request,
                "validation_error",
                "请求参数无效",
                False,
                jsonable_encoder(exc.errors()),
            ),
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        codes = {
            404: "not_found",
            405: "method_not_allowed",
            413: "request_too_large",
        }
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=error_body(
                request,
                codes.get(exc.status_code, "http_error"),
                message,
                exc.status_code >= 500,
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=error_body(
                request,
                "internal_error",
                "服务器内部错误",
                True,
            ),
        )
