"""通过 Codex Responses 通道调用官方目录确认支持的文本模型。"""
from __future__ import annotations

import base64
from collections.abc import Iterable, Iterator
import json

from services.codex_client import codex_headers
from services.openai_backend_api import OpenAIBackendAPI


class CodexTextError(RuntimeError):
    """只携带固定公开错误；不保存上游响应正文或账号凭据。"""

    def __init__(self, message: str, code: str = "upstream_error", status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def to_openai_error(self) -> dict[str, object]:
        return {"error": {
            "message": str(self),
            "type": "invalid_request_error" if self.status_code == 400 else "rate_limit_error" if self.status_code == 429 else "server_error",
            "param": None,
            "code": self.code,
        }}


def _invalid_request() -> CodexTextError:
    return CodexTextError(
        "The Codex text channel supports system, developer, user and assistant messages with text and user images.",
        "unsupported_message_content", 400,
    )


def _invalid_response() -> CodexTextError:
    return CodexTextError("The Codex text channel returned an invalid response.", "invalid_upstream_response")


def _content_parts(content: object, role: str) -> list[dict[str, object]]:
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    if not isinstance(content, list):
        raise _invalid_request()
    parts: list[dict[str, object]] = []
    for part in content:
        if not isinstance(part, dict):
            raise _invalid_request()
        part_type = part.get("type")
        if not isinstance(part_type, str):
            raise _invalid_request()
        if part_type in {"text", "input_text", "output_text"}:
            text = part.get("text")
            if not isinstance(text, str):
                raise _invalid_request()
            parts.append({"type": text_type, "text": text})
        elif part_type == "image" and role == "user":
            data, mime = part.get("data"), part.get("mime")
            if not isinstance(data, (bytes, bytearray)) or not data or not isinstance(mime, str) or not mime.startswith("image/"):
                raise _invalid_request()
            encoded = base64.b64encode(bytes(data)).decode("ascii")
            parts.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})
        else:
            raise _invalid_request()
    return parts


def build_codex_text_payload(
    messages: list[dict[str, object]], model: str, thinking_effort: str = "",
) -> dict[str, object]:
    """接收 conversation.normalize_messages 的结果，保留消息角色与顺序。"""
    instructions: list[str] = []
    input_items: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise _invalid_request()
        role = message.get("role")
        if not isinstance(role, str) or role not in {"system", "developer", "user", "assistant"}:
            raise _invalid_request()
        parts = _content_parts(message.get("content"), str(role))
        if role in {"system", "developer"}:
            instructions.append("".join(str(part["text"]) for part in parts))
        else:
            input_items.append({"type": "message", "role": role, "content": parts})
    payload: dict[str, object] = {
        "model": model,
        "instructions": "\n\n".join(instructions),
        "input": input_items,
        "store": False,
        "stream": True,
    }
    if thinking_effort:
        # 既有网页通道称 extended；Codex Responses 使用 xhigh。
        effort = "xhigh" if thinking_effort == "extended" else thinking_effort
        if effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise _invalid_request()
        payload["reasoning"] = {"effort": effort}
    return payload


def iter_codex_sse_events(lines: Iterable[bytes | str]) -> Iterator[dict[str, object]]:
    """逐帧解析 SSE，支持多行 data 与未带末尾空行的最后一帧。"""
    data_lines: list[str] = []
    for raw_line in lines:
        try:
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        except UnicodeDecodeError:
            raise _invalid_response() from None
        if not isinstance(line, str):
            raise _invalid_response()
        line = line.rstrip("\r\n")
        if not line:
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            data_lines = []
            if payload.strip() == "[DONE]":
                return
            yield _decode_sse_payload(payload)
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        payload = "\n".join(data_lines)
        if payload.strip() != "[DONE]":
            yield _decode_sse_payload(payload)


def _decode_sse_payload(payload: str) -> dict[str, object]:
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        raise _invalid_response() from None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise _invalid_response()
    return event


def _event_error(event: dict[str, object]) -> CodexTextError:
    response = event.get("response")
    error = event.get("error")
    if not isinstance(error, dict) and isinstance(response, dict):
        error = response.get("error")
    code = error.get("code") if isinstance(error, dict) else event.get("code")
    if isinstance(code, str) and code in {"token_invalidated", "token_revoked", "invalid_token", "invalid_api_key"}:
        return CodexTextError("The upstream authentication token has been invalidated.", "token_invalidated")
    if isinstance(code, str) and code in {"rate_limit_exceeded", "usage_limit_reached", "quota_exceeded"}:
        return CodexTextError("The selected account has reached the upstream model usage limit.", "upstream_rate_limit", 429)
    return CodexTextError("The Codex text response did not complete successfully.", "upstream_response_failed")


def _completed_text(response: object) -> str:
    if not isinstance(response, dict) or response.get("status") != "completed" or response.get("error"):
        raise _invalid_response()
    output = response.get("output")
    if not isinstance(output, list):
        raise _invalid_response()
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            raise _invalid_response()
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise CodexTextError("The Codex text channel returned unsupported output.", "unsupported_upstream_output")
        content = item.get("content")
        if not isinstance(content, list):
            raise _invalid_response()
        for part in content:
            if not isinstance(part, dict):
                raise _invalid_response()
            if part.get("type") == "refusal":
                raise CodexTextError("The upstream model declined the text request.", "upstream_refusal")
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise _invalid_response()
            texts.append(part["text"])
    text = "".join(texts)
    if not text:
        raise CodexTextError("The Codex text channel completed without a text response.", "empty_upstream_response")
    return text


def codex_text_deltas(events: Iterable[dict[str, object]]) -> Iterator[str]:
    """只转发可见文本；截断、失败和取消不能产生成功完成结果。"""
    full_text = ""
    for event in events:
        event_type = event.get("type")
        if event_type in {"error", "response.failed", "response.incomplete", "response.cancelled", "response.canceled"}:
            raise _event_error(event)
        if event_type in {"response.refusal.delta", "response.refusal.done"}:
            raise CodexTextError("The upstream model declined the text request.", "upstream_refusal")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise _invalid_response()
            if delta:
                full_text += delta
                yield delta
        elif event_type in {"response.completed", "response.done"}:
            text = _completed_text(event.get("response"))
            if not text.startswith(full_text):
                raise _invalid_response()
            remaining = text[len(full_text):]
            if remaining:
                yield remaining
            return
    raise CodexTextError("The Codex text stream ended before completion.", "incomplete_upstream_response")


class CodexTextBackend:
    """复用项目会话/代理设置，使用独立的 Codex 身份头，不进行网页 bootstrap。"""

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token
        self._backend: OpenAIBackendAPI | None = None

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()

    def iter_text_deltas(
        self, messages: list[dict[str, object]], model: str, thinking_effort: str = "",
    ) -> Iterator[str]:
        if not self.access_token:
            raise CodexTextError("An authenticated account is required for the Codex text channel.", "model_unavailable")
        payload = build_codex_text_payload(messages, model, thinking_effort)
        response = None
        try:
            if self._backend is None:
                self._backend = OpenAIBackendAPI(access_token=self.access_token)
            response = self._backend.session.post(
                self._backend.base_url + "/backend-api/codex/responses",
                json=payload,
                headers=codex_headers(self.access_token, self._backend.account, accept="text/event-stream"),
                timeout=(30, 300),
                stream=True,
            )
            status = response.status_code
            if status == 401:
                raise CodexTextError("The upstream authentication token has been invalidated.", "token_invalidated")
            if status == 429:
                raise CodexTextError("The selected account has reached the upstream model usage limit.", "upstream_rate_limit", 429)
            if not 200 <= status < 300:
                raise CodexTextError("The Codex text request failed upstream.", f"upstream_http_{status}")
            if "text/event-stream" not in str(response.headers.get("content-type") or "").lower():
                raise _invalid_response()
            yield from codex_text_deltas(iter_codex_sse_events(response.iter_lines()))
        except CodexTextError:
            raise
        except Exception:
            # 网络异常可能包含代理用户名、URL 或令牌；不透出原始异常。
            raise CodexTextError("The Codex text connection failed. Please retry later.", "upstream_connection_failed") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
