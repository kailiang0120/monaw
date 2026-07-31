from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class ApiErrorEnvelope(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


def error_code_for_status(status_code: int) -> str:
    return _STATUS_CODES.get(status_code, "request_failed")


def request_id_for(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or request.headers.get("x-request-id") or "")


def api_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    envelope = ApiErrorEnvelope(
        code=code,
        message=message,
        request_id=request_id,
        details=details or {},
    )
    response = JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(),
        headers=headers,
    )
    if request_id:
        response.headers["X-Request-Id"] = request_id
    return response


def _message_for_status(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Request failed"


def _detail_message(detail: Any, status_code: int) -> str:
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    return _message_for_status(status_code)


def _detail_code(detail: Any, status_code: int) -> str:
    if isinstance(detail, dict):
        code = detail.get("code")
        if isinstance(code, str) and code:
            return code
    return error_code_for_status(status_code)


def _detail_payload(detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        payload = detail.get("details")
        return payload if isinstance(payload, dict) else {}
    return {}


def _validation_details(exc: RequestValidationError) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    for error in exc.errors():
        errors.append(
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "type": str(error.get("type", "")),
                "message": str(error.get("msg", "")),
            }
        )
    return {"errors": errors}


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return api_error_response(
        status_code=exc.status_code,
        code=_detail_code(exc.detail, exc.status_code),
        message=_detail_message(exc.detail, exc.status_code),
        request_id=request_id_for(request),
        details=_detail_payload(exc.detail),
        headers=exc.headers,
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return api_error_response(
        status_code=422,
        code="validation_error",
        message="Request validation failed",
        request_id=request_id_for(request),
        details=_validation_details(exc),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return api_error_response(
        status_code=500,
        code="internal_error",
        message="Internal server error",
        request_id=request_id_for(request),
    )
