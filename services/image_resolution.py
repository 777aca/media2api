"""Shared image size contract (OpenAI GPT Image 2 / 2.5)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3840
DEFAULT_CALIBRATION = {"enabled": False, "worker_url": "http://127.0.0.1:3310", "timeout_secs": 300}


class ImageSizeError(ValueError):
    status_code = 400

    def to_openai_error(self) -> dict[str, object]:
        return {"error": {"message": str(self), "type": "invalid_request_error", "code": "invalid_size", "param": "size"}}


def validate_image_size(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ImageSizeError("size 必须是 auto 或 WIDTHxHEIGHT")
    value = value.strip().lower()
    if value == "auto":
        return value
    match = re.fullmatch(r"([0-9]{1,4})x([0-9]{1,4})", value)
    if not match:
        raise ImageSizeError("size 必须是 auto 或 WIDTHxHEIGHT")
    width, height = map(int, match.groups())
    if min(width, height) <= 0 or max(width, height) > MAX_EDGE or width % 16 or height % 16:
        raise ImageSizeError("图片宽高必须是 16 的正整数倍，且不超过 3840 像素")
    if max(width, height) > 3 * min(width, height):
        raise ImageSizeError("图片长短边比例不能超过 3:1")
    if not MIN_PIXELS <= width * height <= MAX_PIXELS:
        raise ImageSizeError("图片总像素必须在 655,360 至 8,294,400 之间")
    return f"{width}x{height}"


def parse_dimensions(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([0-9]{1,5})x([0-9]{1,5})", value)
    if not match:
        return None
    width, height = map(int, match.groups())
    return (width, height) if width > 0 and height > 0 else None


def calibration_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    settings = {**DEFAULT_CALIBRATION, **{k: v for k, v in source.items() if k in DEFAULT_CALIBRATION}}
    if not isinstance(settings["enabled"], bool):
        raise ValueError("分辨率校准开关必须为布尔值")
    url = settings["worker_url"]
    if not isinstance(url, str):
        raise ValueError("超分服务地址必须为 HTTP(S) URL")
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("超分服务地址必须为不含凭据和查询参数的 HTTP(S) URL")
    settings["worker_url"] = url.strip().rstrip("/")
    timeout = settings["timeout_secs"]
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError("超分超时必须是 1 至 600 的整数秒")
    return settings
