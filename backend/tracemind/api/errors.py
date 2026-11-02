"""Error envelope. Responses carry stable codes and field-level problems only — never stack
traces, SQL, paths, configuration, or echoes of submitted values."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy.exc
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from tracemind.observability.logging import LOGGER_NAME, log

_logger = logging.getLogger(LOGGER_NAME + ".api")

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
}


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        self.headers = headers or {}


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        rid = request.state.request_id = uuid.uuid4().hex
    return str(rid)


def envelope(
    request: Request,
    status: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    rid = request_id(request)
    body = {
        "error": {"code": code, "message": message, "details": details or [], "request_id": rid}
    }
    return JSONResponse(
        status_code=status, content=body, headers={**(headers or {}), "X-Request-ID": rid}
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return envelope(request, exc.status, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": [str(p) for p in e["loc"]], "type": e["type"]} for e in exc.errors()]
        return envelope(request, 422, "validation_error", "Request validation failed", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        return envelope(request, exc.status_code, code, code.replace("_", " ").capitalize())

    @app.exception_handler(sqlalchemy.exc.OperationalError)
    @app.exception_handler(sqlalchemy.exc.InterfaceError)
    async def _storage(request: Request, exc: Exception) -> JSONResponse:
        log(
            _logger,
            logging.ERROR,
            "storage unavailable",
            request_id=request_id(request),
            error=type(exc).__name__,
        )
        return envelope(request, 503, "storage_unavailable", "Storage is temporarily unavailable")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Only the class name is logged: exception text can carry SQL parameters or payload data.
        log(
            _logger,
            logging.ERROR,
            "unhandled error",
            request_id=request_id(request),
            error=type(exc).__name__,
        )
        return envelope(request, 500, "internal_error", "Internal error")
