"""Restricted reverse proxy for a separately managed grok2api instance."""
from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from starlette.formparsers import MultiPartException, MultiPartParser
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException
from utils.helper import parse_image_count


_ID = r"(?!\.{1,2}(?:/|$))[A-Za-z0-9_.~-]{1,256}"
_HTTP_PATHS = {
    "GET": (
        r"models", rf"responses/{_ID}", rf"videos/{_ID}(?:/content)?",
        r"tts/voices", rf"tts/voices/{_ID}", rf"media/(?:images|videos)/{_ID}",
    ),
    "HEAD": (rf"media/(?:images|videos)/{_ID}",),
    "POST": (
        r"chat/completions", r"responses(?:/compact)?", r"messages",
        r"images/(?:generations|edits)", r"videos/(?:generations|edits|extensions)",
        r"tts", r"stt", r"audio/(?:speech|tasks|transcriptions)",
    ),
    "DELETE": (rf"responses/{_ID}",),
    "PUT": (r"media/uploads/[0-9a-fA-F]{64}",),
}
_WS_PATHS = {"realtime", "stt"}
_IMAGE_PATHS = {"images/generations", "images/edits"}
_IMAGE_BODY_LIMIT = 32 * 1024 * 1024
_HOP_HEADERS = {
    "connection", "host", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length", "cookie",
    "set-cookie", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "location",
}


def _gateway_url(path: str, query: str = "", *, websocket: bool = False) -> str:
    configured = os.environ.get("MEDIA2API_GROK_GATEWAY_URL", "").strip().rstrip("/")
    try:
        parts = urlsplit(configured)
        _ = parts.port
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Grok 网关未配置或地址无效") from exc
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise HTTPException(status_code=503, detail="Grok 网关未配置或地址无效")
    scheme = {"http": "ws", "https": "wss"}[parts.scheme] if websocket else parts.scheme
    return urlunsplit((scheme, parts.netloc, f"{parts.path}/v1/{path}", query, ""))


def _allowed(method: str, path: str) -> bool:
    return any(re.fullmatch(pattern, path) for pattern in _HTTP_PATHS.get(method, ()))


def _forward_headers(headers, *, websocket: bool = False) -> list[tuple[str, str]]:
    connection = set()
    for key, value in headers.items():
        if key.lower() == "connection":
            connection.update(part.strip().lower() for part in value.split(","))
    return [
        (key, value) for key, value in headers.items()
        if key.lower() not in _HOP_HEADERS | connection
        and not key.lower().startswith("sec-websocket-")
        and (not websocket or key.lower() != "origin")
    ]


async def _image_body(request: Request) -> bytes:
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _IMAGE_BODY_LIMIT:
            raise HTTPException(status_code=413, detail="图片请求体超过 32 MiB")
        chunks.append(chunk)
    body = b"".join(chunks)
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("application/json"):
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="图片请求不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="图片请求必须是 JSON 对象")
        parse_image_count(payload.get("n"))
    elif content_type.startswith("multipart/form-data"):
        async def stream():
            yield body

        try:
            form = await MultiPartParser(request.headers, stream(), max_files=20, max_fields=100).parse()
        except MultiPartException as exc:
            raise HTTPException(status_code=400, detail="图片表单格式无效") from exc
        try:
            for count in form.getlist("n"):
                parse_image_count(count)
        finally:
            await form.close()
    else:
        raise HTTPException(status_code=415, detail="图片请求需要 JSON 或 multipart/form-data")
    return body


class _NoRedirectConnect(connect):
    # Do not send a caller's Grok credentials to a redirected host.
    def process_redirect(self, exc: Exception) -> Exception:
        return exc


def create_router() -> APIRouter:
    router = APIRouter()

    @router.api_route("/grok/v1/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE"])
    async def grok_http(path: str, request: Request):
        query = request.url.query
        if any(path.startswith(f"media/{kind}/") for kind in ("uploads", "images", "videos")):
            # Upload tickets and public asset URLs grant access; Uvicorn logs scope.path.
            safe_path = f"/grok/v1/media/{path.split('/')[1]}/[REDACTED]"
            request.scope["path"] = safe_path
            request.scope["raw_path"] = safe_path.encode("ascii")
            request.scope["query_string"] = b""
        if not _allowed(request.method, path):
            raise HTTPException(status_code=404, detail="Not Found")
        url = _gateway_url(path, query)
        content = await _image_body(request) if path in _IMAGE_PATHS else (
            request.stream() if request.method in {"POST", "PUT"} else None
        )
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=7200, write=120, pool=10),
            follow_redirects=False, trust_env=False,
        )
        try:
            outgoing = client.build_request(
                request.method, url, headers=_forward_headers(request.headers),
                content=content,
            )
            response = await client.send(outgoing, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(status_code=502, detail="Grok 网关暂时不可用") from exc
        except BaseException:
            await client.aclose()
            raise

        async def body():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            body(), status_code=response.status_code,
            headers=dict(_forward_headers(response.headers)),
        )

    @router.websocket("/grok/v1/{path:path}")
    async def grok_websocket(path: str, websocket: WebSocket):
        if path not in _WS_PATHS:
            await websocket.close(code=1008)
            return
        try:
            url = _gateway_url(path, websocket.url.query, websocket=True)
        except HTTPException:
            await websocket.close(code=1013)
            return
        try:
            async with _NoRedirectConnect(
                url, additional_headers=_forward_headers(websocket.headers, websocket=True),
                subprotocols=websocket.scope.get("subprotocols") or None,
                proxy=None, open_timeout=10, close_timeout=3, max_size=32 * 1024 * 1024,
            ) as upstream:
                await websocket.accept(subprotocol=upstream.subprotocol)

                async def send_upstream():
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("text") is not None:
                            await upstream.send(message["text"])
                        elif message.get("bytes") is not None:
                            await upstream.send(message["bytes"])

                async def send_downstream():
                    async for message in upstream:
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)

                tasks = [asyncio.create_task(send_upstream()), asyncio.create_task(send_downstream())]
                try:
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*done, *pending, return_exceptions=True)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await websocket.close()
                except (RuntimeError, WebSocketDisconnect):
                    pass
        except InvalidStatus as exc:
            try:
                await websocket.close(code=1008 if exc.response.status_code in {401, 403} else 1013)
            except (RuntimeError, WebSocketDisconnect):
                pass
        except (OSError, TimeoutError, WebSocketException, ValueError):
            try:
                await websocket.close(code=1013)
            except (RuntimeError, WebSocketDisconnect):
                pass

    return router
