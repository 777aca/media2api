from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.protocol.error_response import anthropic_error_response, openai_error_response


def _is_openai_compatible_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


def _is_anthropic_messages_path(path: str) -> bool:
    return path == "/v1/messages"


def _compatible_error_response(
    request: Request,
    detail: object,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    if _is_anthropic_messages_path(request.url.path):
        return anthropic_error_response(detail, status_code, headers=headers)
    return openai_error_response(detail, status_code, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    from services.generation_errors import GenerationRuntimeError

    @app.exception_handler(GenerationRuntimeError)
    async def generation_runtime_error(request: Request, exc: GenerationRuntimeError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_openai_error())

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if _is_openai_compatible_path(request.url.path):
            return _compatible_error_response(request, exc.detail, exc.status_code, exc.headers)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": jsonable_encoder(exc.detail)},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path in {"/v1/images/generations", "/v1/images/edits", "/v1/chat/completions", "/v1/responses", "/api/image-tasks/generations", "/api/image-tasks/edits"}:
            from api.support import require_identity
            from services.generation_protocol import record_image_rejection
            try:
                identity = require_identity(request.headers.get("authorization"))
            except StarletteHTTPException:
                identity = None
            if identity:
                record_image_rejection(identity, request.url.path, exc.body if isinstance(exc.body, dict) else {}, exc, category="invalid_request")
        if _is_openai_compatible_path(request.url.path):
            return _compatible_error_response(request, exc.errors(), 422)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
