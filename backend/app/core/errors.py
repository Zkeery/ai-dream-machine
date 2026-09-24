# -*- coding: utf-8 -*-
"""统一错误模型：所有接口错误都返回 {"error": {"code", "message"}}，不泄露堆栈。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    """业务错误，携带 code 与用户可读 message。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=error_body("VALIDATION_ERROR", "请求参数不合法"))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        # 兜底：不泄露堆栈，只返回通用错误码
        return JSONResponse(status_code=500, content=error_body("INTERNAL_ERROR", "服务内部错误"))
