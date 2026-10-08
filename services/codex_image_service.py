"""Flare / Sunburst 的独立 Codex Images 协议，不降级或借用网页图片入口。"""
from __future__ import annotations

import base64
import binascii
from io import BytesIO
import warnings

from PIL import Image

from services.codex_client import codex_headers
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import WEB_IMAGE_MODELS_25


_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_INPUT_IMAGE_MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


class CodexImageError(RuntimeError):
    """只保存固定公开错误；原始响应和请求凭据不进入异常或日志。"""

    def __init__(self, message: str, *, status_code: int = 502, code: str = "upstream_error",
                 error_type: str = "server_error", retryable_connection: bool = False,
                 upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.error_type = error_type
        self.retryable_connection = retryable_connection
        self.upstream_status = upstream_status


def _invalid_input() -> CodexImageError:
    return CodexImageError("Image and mask inputs must contain valid image data.", status_code=400,
                           code="invalid_image_input", error_type="invalid_request_error")


def _invalid_output() -> CodexImageError:
    return CodexImageError("The selected image model returned invalid PNG image data.", code="invalid_upstream_image")


def _validated_image(value: object, *, output: bool = False) -> tuple[bytes, str]:
    error = _invalid_output if output else _invalid_input
    if not isinstance(value, str) or not value.strip():
        raise error()
    encoded = value.strip()
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or not header.startswith("data:image/") or not header.endswith(";base64"):
            raise error()
    if len(encoded) > ((_MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise error()
    try:
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise ValueError("invalid image length")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image_format = image.format
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
        mime = _INPUT_IMAGE_MIME_TYPES.get(str(image_format))
        if mime is None or (output and image_format != "PNG"):
            raise ValueError("unsupported image encoding")
    except (binascii.Error, ValueError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise error() from None
    return data, mime


def _image_data_uri(value: str) -> str:
    data, mime = _validated_image(value)
    # encode_images 的 bare Base64 会丢失 MIME，以真实字节恢复 JPEG / WebP / PNG 类型。
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _mask_data_uri(value: str, first_image: str) -> str:
    try:
        data, mime = _validated_image(value)
        original, _ = _validated_image(first_image)
        with Image.open(BytesIO(data)) as mask, Image.open(BytesIO(original)) as reference:
            if mime != "image/png" or mask.size != reference.size or ("A" not in mask.getbands() and "transparency" not in mask.info):
                raise ValueError("invalid mask")
    except (CodexImageError, OSError, ValueError):
        raise CodexImageError("mask must be a PNG with transparency and match the first image dimensions.",
                              status_code=400, code="invalid_mask", error_type="invalid_request_error") from None
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


def build_codex_image_request(*, model: str, prompt: str, images: list[str] | None = None,
                             mask: str | None = None, size: str | None = None,
                             quality: str = "auto") -> tuple[str, dict[str, object]]:
    model = model.strip()
    if model not in WEB_IMAGE_MODELS_25:
        raise CodexImageError("Unsupported image model. Use gpt-image-2.5-flare or gpt-image-2.5-sunburst.",
                              status_code=400, code="unsupported_image_model", error_type="invalid_request_error")
    if not prompt.strip():
        raise CodexImageError("prompt is required", status_code=400, code="invalid_prompt", error_type="invalid_request_error")
    payload: dict[str, object] = {"model": model, "prompt": prompt, "output_format": "png", "n": 1}
    if size:
        payload["size"] = size
    if quality:
        payload["quality"] = quality
    path = "/backend-api/codex/images/generations"
    if images:
        path = "/backend-api/codex/images/edits"
        payload["images"] = [{"image_url": _image_data_uri(image)} for image in images]
        if mask:
            payload["mask"] = {"image_url": _mask_data_uri(mask, images[0])}
    elif mask:
        raise CodexImageError("A mask requires an image input.", status_code=400, code="invalid_mask", error_type="invalid_request_error")
    return path, payload


def _classify_upstream_error(status: int, payload: object) -> CodexImageError:
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if status == 401 or (isinstance(code, str) and code in {"token_invalidated", "token_revoked", "invalid_token", "invalid_api_key"}):
        return CodexImageError("The upstream authentication token has been invalidated.", code="token_invalidated")
    if status == 429 or (isinstance(code, str) and code in {"rate_limit_exceeded", "usage_limit_reached", "quota_exceeded"}):
        return CodexImageError("The selected account has reached the image model usage limit.", status_code=429,
                               code="upstream_rate_limit", error_type="rate_limit_error")
    if isinstance(code, str) and code in {"content_policy_violation", "moderation_blocked", "content_filter"}:
        return CodexImageError("The image request was rejected by upstream content policy.", status_code=400,
                               code="content_policy_violation", error_type="invalid_request_error")
    if isinstance(code, str) and code in {"model_not_found", "model_not_available"}:
        return CodexImageError("上游明确拒绝了所选图片型号，当前账号无法使用该型号。", status_code=403,
                               code="model_permission_denied", error_type="permission_error")
    if status == 403 or code == "permission_denied":
        status_hint = "（HTTP 403）" if status == 403 else ""
        return CodexImageError(f"上游拒绝访问 Codex 独立图片接口{status_hint}。可改用 gpt-image-2 网页入口，或使用具备该接口权限的账号。",
                               status_code=403, code="upstream_access_denied", error_type="permission_error")
    if status == 404:
        return CodexImageError("上游图片接口不存在或未开放（HTTP 404），请检查图片通道。",
                               code="upstream_endpoint_not_found")
    if status in {400, 422}:
        return CodexImageError("上游拒绝了图片请求参数，请检查尺寸、质量和参考图。", status_code=400,
                               code="invalid_image_request", error_type="invalid_request_error")
    return CodexImageError("The image model request failed upstream.", code=f"upstream_http_{status}" if status != 200 else "upstream_error")


def _upstream_error(status: int, payload: object) -> CodexImageError:
    error = _classify_upstream_error(status, payload)
    error.upstream_status = status
    return error


def parse_codex_image_response(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        raise _invalid_output()
    if payload.get("error"):
        raise _upstream_error(200, payload)
    items = payload.get("data")
    if not isinstance(items, list):
        raise _invalid_output()
    if not items:
        raise CodexImageError("The selected image model returned no image output.", code="empty_upstream_image")
    result: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise _invalid_output()
        data, _mime = _validated_image(item.get("b64_json"), output=True)
        revised_prompt = item.get("revised_prompt")
        if revised_prompt is not None and not isinstance(revised_prompt, str):
            raise _invalid_output()
        result.append({"b64_json": base64.b64encode(data).decode("ascii"), "revised_prompt": revised_prompt or ""})
    return result


def generate_codex_image(backend: OpenAIBackendAPI, *, model: str, prompt: str,
                         images: list[str] | None = None, mask: str | None = None,
                         size: str | None = None, quality: str = "auto") -> list[dict[str, str]]:
    """每次只生成一张；n / 账号并发 / 结算由既有图片池负责。"""
    path, payload = build_codex_image_request(model=model, prompt=prompt, images=images, mask=mask, size=size, quality=quality)
    if not backend.access_token:
        raise CodexImageError("An authenticated account is required for this image model.", code="model_unavailable")
    response = None
    try:
        response = backend.session.post(
            backend.base_url + path, json=payload,
            headers=codex_headers(backend.access_token, backend.account), timeout=(30, 1200),
        )
        try:
            response_payload = response.json()
        except Exception:
            response_payload = None
        if not 200 <= response.status_code < 300:
            raise _upstream_error(response.status_code, response_payload)
        images_result = parse_codex_image_response(response_payload)
        if len(images_result) != 1:
            raise CodexImageError("The image model returned an unexpected image count.", code="invalid_upstream_image_count")
        return images_result
    except CodexImageError:
        raise
    except Exception as exc:
        # 仅 DNS、TCP 连接及 TLS 握手失败确认尚未提交请求；读取超时不能自动重发。
        retryable = getattr(exc, "code", None) in {5, 6, 7, 35, 60}
        raise CodexImageError("The image model connection failed. Please retry later.",
                              code="upstream_connection_failed", retryable_connection=retryable) from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
