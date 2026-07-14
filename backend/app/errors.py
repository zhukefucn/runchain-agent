from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response


REQUEST_ID_HEADER = "X-Request-ID"


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error_response(
    request: Request, status_code: int, code: str, message: str
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid4()))
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": request_id,
        },
        headers={REQUEST_ID_HEADER: request_id},
    )


async def api_error_handler(request: Request, error: ApiError) -> JSONResponse:
    return _error_response(request, error.status_code, error.code, error.message)


async def validation_error_handler(
    request: Request, _error: RequestValidationError
) -> JSONResponse:
    return _error_response(request, 422, "VALIDATION_ERROR", "请求参数无效")


async def http_error_handler(
    request: Request, error: StarletteHTTPException
) -> JSONResponse:
    if error.status_code == 404:
        return _error_response(request, 404, "NOT_FOUND", "资源不存在")
    return _error_response(request, error.status_code, "HTTP_ERROR", "请求失败")


async def internal_error_handler(
    request: Request, _error: Exception
) -> JSONResponse:
    return _error_response(request, 500, "INTERNAL_ERROR", "服务器内部错误")


async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)
    app.middleware("http")(request_id_middleware)
