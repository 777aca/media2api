"""请求参数的独立日志快照：脱敏、图片摘要与体积限制。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import math
import re
import warnings
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urlsplit, urlunsplit

from PIL import Image
from starlette.datastructures import UploadFile

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
MAX_STRING_CHARS = 16_000
MAX_TOTAL_CHARS = 64_000
MAX_NODES = 1_000
MAX_ITEMS = 100
MAX_DEPTH = 10
# 整个快照共享预算，避免多个素材累计消耗无界内存和 CPU。
MAX_ASSET_BYTES = 20 * 1024 * 1024

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
    """返回独立 JSON 快照；仅摘要已有字节，不读取上传流或请求外部 URL。"""
    remaining_chars = MAX_TOTAL_CHARS
    remaining_nodes = MAX_NODES
    remaining_asset_bytes = MAX_ASSET_BYTES

    def text(value: str) -> str:
        nonlocal remaining_chars
        # 先脱敏再截断，避免截断后留下部分签名或图片内容。
        value = _DATA_URI.sub(lambda match: f"[data:{match.group(1) or 'unknown'}; content omitted]", value)
        value = _URL.sub(_redact_url, value)
        limit = max(0, min(MAX_STRING_CHARS, remaining_chars))
        remaining_chars -= min(len(value), limit)
        return value if len(value) <= limit else value[:limit] + TRUNCATED

    def mime(value: str) -> str | None:
        candidate = value.split(";", 1)[0].strip().lower()
        if len(candidate) <= 127 and re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", candidate):
            return candidate
        return None

    def binary_summary(value: bytes | bytearray | memoryview, summary: dict[str, object]) -> dict[str, object]:
        nonlocal remaining_asset_bytes
        size = value.nbytes if isinstance(value, memoryview) else len(value)
        summary["size_bytes"] = size
        if size > remaining_asset_bytes:
            summary["decode_status"] = "omitted_too_large"
            return summary
        remaining_asset_bytes -= size
        raw = bytes(value)
        summary.update(sha256=hashlib.sha256(raw).hexdigest(), decode_status="valid")
        try:
            # open 只解析头部；禁止调用 load/verify/转换等像素解码操作。
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as picture:
                    detected_mime = Image.MIME.get(picture.format)
                    if detected_mime:
                        summary["mime_type"] = detected_mime
                    summary.update(width=picture.width, height=picture.height)
        except Exception:
            # 任意损坏或不支持的图片不能打断日志，字节指纹仍然有效。
            pass
        return summary

    def encoded_summary(value: str) -> dict[str, object]:
        summary: dict[str, object] = {"type": "base64", "length": len(value), "content_omitted": True}
        is_base64 = True
        if value[:5].lower() == "data:":
            # 限制头部扫描和切片；未解析的输入绝不回写日志。
            separator = value.find(",", 0, 1024)
            if separator < 0:
                summary["decode_status"] = "invalid"
                return summary
            header = value[5:separator]
            declared_mime = mime(header)
            if declared_mime:
                summary["mime_type"] = declared_mime
            is_base64 = header.lower().endswith(";base64")
            payload_start = separator + 1
        else:
            payload_start = 0
        # 在复制/清洗/解码前限制编码文本长度（空白也计入上限）。
        encoded_limit = 4 * ((remaining_asset_bytes + 2) // 3) if is_base64 else 3 * remaining_asset_bytes
        if len(value) - payload_start > encoded_limit:
            summary["decode_status"] = "omitted_too_large"
            return summary
        payload = value[payload_start:]
        try:
            if is_base64:
                payload = re.sub(r"[ \t\r\n]", "", payload)
                estimated_size = len(payload) // 4 * 3 - min(2, len(payload) - len(payload.rstrip("=")))
                if estimated_size > remaining_asset_bytes:
                    summary["decode_status"] = "omitted_too_large"
                    return summary
                raw = base64.b64decode(payload, validate=True)
            else:
                if re.search(r"%(?![0-9a-fA-F]{2})", payload):
                    raise ValueError("Invalid escape")
                encoded_size = len(payload) if payload.isascii() else len(payload.encode("utf-8"))
                if encoded_size - 2 * payload.count("%") > remaining_asset_bytes:
                    summary["decode_status"] = "omitted_too_large"
                    return summary
                raw = unquote_to_bytes(payload)
        except (ValueError, binascii.Error):
            summary["decode_status"] = "invalid"
            return summary
        return binary_summary(raw, summary)

    def visit(value: object, name: str = "", depth: int = 0) -> object:
        nonlocal remaining_nodes
        remaining_nodes -= 1
        if remaining_nodes < 0 or remaining_chars <= 0 or depth > MAX_DEPTH:
            return TRUNCATED
        if _is_secret(name):
            return REDACTED
        if isinstance(value, UploadFile):
            summary = {"type": "file", "filename": text(value.filename or ""),
                       "content_type": text(value.content_type or ""), "content_omitted": True}
            if value.size is not None:
                summary["size_bytes"] = value.size
            if declared_mime := mime(value.content_type or ""):
                summary["mime_type"] = declared_mime
            return summary
        if isinstance(value, (bytes, bytearray, memoryview)):
            return binary_summary(value, {"type": "binary", "length": len(value), "content_omitted": True})
        if isinstance(value, str):
            key = _key(name)
            # 小图片也可能不足 256 字符；URL 的冒号等字符不属于 base64 字符集。
            image_data = (key in {"image", "images", "mask"} or (key == "data" and len(value) > 256))
            if value[:5].lower() == "data:" or key in _BINARY_KEYS or (image_data and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value)):
                return encoded_summary(value)
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
                summary = {"type": "file", "filename": text(str(value[1])), "content_type": text(str(value[2])),
                           "length": len(value[0]), "content_omitted": True}
                if declared_mime := mime(str(value[2])):
                    summary["mime_type"] = declared_mime
                return binary_summary(value[0], summary)
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
