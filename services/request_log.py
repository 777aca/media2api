"""请求参数的独立日志快照：脱敏、图片摘要与体积限制。"""
from __future__ import annotations

import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.datastructures import UploadFile

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
MAX_STRING_CHARS = 16_000
MAX_TOTAL_CHARS = 64_000
MAX_NODES = 1_000
MAX_ITEMS = 100
MAX_DEPTH = 10

_SECRET_KEYS = {
    "authorization", "proxyauthorization", "cookie", "setcookie", "cookies",
    "apikey", "xapikey", "authkey", "password", "passwd", "secret", "clientsecret",
    "token", "accesstoken", "refreshtoken", "idtoken", "sessiontoken", "sessionkey",
    "credential", "credentials", "signature", "sig", "key",
}
_BINARY_KEYS = {"base64", "b64json", "base64images", "imagebase64", "imagedata", "b64"}
_DATA_URI = re.compile(r"data:([^;,\s]+)?(?:;[^,\s]*)?,[^\s\"'<>)]*", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _is_secret(value: str) -> bool:
    key = _key(value)
    return key in _SECRET_KEYS or key.endswith(("apikey", "password", "secret", "accesstoken", "refreshtoken")) or key.startswith(("xamz", "xgoog"))


def _redact_url(match: re.Match[str]) -> str:
    try:
        url = urlsplit(match.group())
        # 签名和用户信息不落盘；保留普通图片 URL 的路径及尺寸等查询参数。
        netloc = url.netloc.rsplit("@", 1)[-1]
        if "@" in url.netloc:
            netloc = f"{REDACTED}@{netloc}"
        query = urlencode([(name, REDACTED if _is_secret(name) else value)
                           for name, value in parse_qsl(url.query, keep_blank_values=True)])
        return urlunsplit((url.scheme, netloc, url.path, query, REDACTED if url.fragment else ""))
    except ValueError:
        return "[URL OMITTED]"


def sanitize_request_parameters(parameters: object) -> dict[str, object]:
    """返回不引用原始可变对象的 JSON 快照，不读取文件内容或记录 HTTP 头。"""
    remaining_chars = MAX_TOTAL_CHARS
    remaining_nodes = MAX_NODES

    def text(value: str) -> str:
        nonlocal remaining_chars
        # 先脱敏再截断，避免截断后留下部分签名或图片内容。
        value = _DATA_URI.sub(lambda match: f"[data:{match.group(1) or 'unknown'}; content omitted]", value)
        value = _URL.sub(_redact_url, value)
        limit = max(0, min(MAX_STRING_CHARS, remaining_chars))
        remaining_chars -= min(len(value), limit)
        return value if len(value) <= limit else value[:limit] + TRUNCATED

    def visit(value: object, name: str = "", depth: int = 0) -> object:
        nonlocal remaining_nodes
        remaining_nodes -= 1
        if remaining_nodes < 0 or remaining_chars <= 0 or depth > MAX_DEPTH:
            return TRUNCATED
        if _is_secret(name):
            return REDACTED
        if isinstance(value, UploadFile):
            return {"type": "file", "filename": text(value.filename or ""),
                    "content_type": text(value.content_type or ""), "size_bytes": value.size, "content_omitted": True}
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"type": "binary", "size_bytes": len(value), "content_omitted": True}
        if isinstance(value, str):
            if _key(name) in _BINARY_KEYS or (_key(name) in {"data", "image", "images", "mask"} and len(value) > 256 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value)):
                return {"type": "base64", "length": len(value), "content_omitted": True}
            return text(value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for index, (child_name, child) in enumerate(value.items()):
                if index >= MAX_ITEMS or remaining_nodes <= 0 or remaining_chars <= 0:
                    result[TRUNCATED] = "Additional parameters omitted"
                    break
                label = str(child_name)
                binary_data = label == "data" and value.get("type") == "base64"
                result[text(label)] = visit(child, "base64" if binary_data else label, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            # 后台任务内部使用 (bytes, filename, content_type) 保存上传文件。
            if isinstance(value, tuple) and len(value) == 3 and isinstance(value[0], bytes):
                return {"type": "file", "filename": text(str(value[1])), "content_type": text(str(value[2])),
                        "size_bytes": len(value[0]), "content_omitted": True}
            result_list: list[object] = []
            for index, child in enumerate(value):
                if index >= MAX_ITEMS or remaining_nodes <= 0 or remaining_chars <= 0:
                    result_list.append(TRUNCATED)
                    break
                result_list.append(visit(child, name, depth + 1))
            return result_list
        return "[UNSUPPORTED VALUE OMITTED]"

    result = visit(parameters)
    return result if isinstance(result, dict) else {}
