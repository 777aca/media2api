"""Calibrate final image bytes once, before storage and API serialization."""
from __future__ import annotations

import io
import os
import time
from contextlib import ExitStack
from dataclasses import dataclass

from curl_cffi import requests
from PIL import Image, ImageOps

from services import fal_esrgan
from services.image_resolution import contained_dimensions, parse_dimensions
from utils.log import logger

MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_DECODE_PIXELS = 40_000_000


@dataclass(frozen=True)
class CalibratedImage:
    data: bytes
    metadata: dict[str, object]


def calibration_mode(actual: tuple[int, int], target: tuple[int, int] | None) -> str:
    if not target or max(actual) >= max(target):
        return "none"
    return "super_resolution" if max(actual) * 3 < max(target) * 2 else "resize"


def _headers() -> dict[str, str]:
    secret = os.environ.get("SUPER_RESOLUTION_WORKER_SECRET", "").strip()
    return {"x-super-resolution-secret": secret} if secret else {}


def worker_ready(settings: dict[str, object]) -> dict[str, object]:
    if settings.get("provider") == "fal":
        if settings.get("fal_api_key"):
            return {"ok": True, "message": "已保存 fal API 密钥，有效性在实际超分时验证。"}
        return {"ok": False, "message": "请填写并保存 fal API 密钥。"}
    try:
        headers = _headers()
        with requests.Session(trust_env=False) as session:
            response = session.get(f"{settings['worker_url']}/health/ready", headers=headers, timeout=5, allow_redirects=False)
            try:
                if response.status_code == 200:
                    return {"ok": True, "message": "超分服务和模型已就绪"}
                if response.status_code == 401:
                    if not headers:
                        return {"ok": False, "message": "超分服务鉴权失败（HTTP 401）。请为主 API 设置 SUPER_RESOLUTION_WORKER_SECRET。"}
                    return {"ok": False, "message": "超分服务鉴权失败（HTTP 401）。请确认主 API 和超分服务的 SUPER_RESOLUTION_WORKER_SECRET 一致。"}
                return {"ok": False, "message": f"超分服务未就绪（HTTP {response.status_code}）"}
            finally:
                response.close()
    except Exception:
        return {"ok": False, "message": "无法连接超分服务，请检查地址、鉴权和模型"}


def super_resolve(data: bytes, settings: dict[str, object], *, target_size: tuple[int, int] | None = None) -> bytes:
    if len(data) > MAX_BODY_BYTES:
        raise ValueError("image_too_large")
    if settings.get("provider") == "fal":
        result = fal_esrgan.upscale(
            data, target_size, int(settings["timeout_secs"]), MAX_BODY_BYTES,
            api_key=str(settings.get("fal_api_key") or ""),
        )
        logger.info({"event": "fal_esrgan_completed", "request_id": result.request_id, "inference_time": result.inference_seconds})
        return result.image
    with requests.Session(trust_env=False) as session:
        response = session.post(
            f"{settings['worker_url']}/v1/super-resolution", data=data,
            headers={**_headers(), "Content-Type": "application/octet-stream", "x-super-resolution-timeout": str(settings["timeout_secs"]),
                     **({"x-super-resolution-size": f"{target_size[0]}x{target_size[1]}"} if target_size else {})},
            timeout=int(settings["timeout_secs"]), allow_redirects=False, stream=True,
        )
        try:
            if response.status_code != 200:
                raise RuntimeError(f"worker_http_{response.status_code}")
            result = bytearray()
            for chunk in response.iter_content():
                result.extend(chunk)
                if len(result) > MAX_BODY_BYTES:
                    raise ValueError("worker_output_too_large")
            return bytes(result)
        finally:
            response.close()


def calibrate_image(data: bytes, requested_size: str | None, settings: dict[str, object]) -> CalibratedImage:
    metadata: dict[str, object] = {"requested_size": requested_size or "auto", "processing": "none", "processing_status": "unchanged"}
    started = time.monotonic()
    mode = "none"
    try:
        with ExitStack() as images:
            with Image.open(io.BytesIO(data)) as opened:
                # Metadata only until the worker returns; waiting requests must
                # not keep decoded source images resident in the API process.
                actual = opened.size
                metadata.update(source_size=f"{actual[0]}x{actual[1]}", actual_size=f"{actual[0]}x{actual[1]}")
                target = parse_dimensions(requested_size)
                mode = calibration_mode(actual, target) if settings["enabled"] else "none"
                if mode == "none" or target is None:
                    return CalibratedImage(data, metadata)
                if len(data) > MAX_BODY_BYTES or actual[0] * actual[1] > MAX_DECODE_PIXELS:
                    raise ValueError("image_too_large")
                has_alpha = "A" in opened.getbands() or "transparency" in opened.info
                if mode == "resize":
                    transposed = images.enter_context(ImageOps.exif_transpose(opened))
                    source = images.enter_context(transposed.convert("RGBA" if has_alpha else "RGB"))
            if mode == "super_resolution":
                upscaled = super_resolve(data, settings, target_size=target)
                # PNG getexif() can itself load all pixels. Read orientation and
                # transparency only after the request has left the worker queue.
                with Image.open(io.BytesIO(data)) as original:
                    oriented_size = actual[::-1] if original.getexif().get(274) in {5, 6, 7, 8} else actual
                full_size = (oriented_size[0] * 4, oriented_size[1] * 4)
                expected_size = contained_dimensions(full_size, target)
                with Image.open(io.BytesIO(upscaled)) as output:
                    if settings.get("provider") == "fal":
                        # Fractional scales may round one edge down by one pixel.
                        fal_size = expected_size if max(expected_size) <= max(full_size) else full_size
                        if any(abs(a - b) > 1 for a, b in zip(output.size, fal_size)):
                            raise ValueError("invalid_fal_dimensions")
                    elif output.size not in {full_size, expected_size}:
                        # Older workers ignore the target header and return full x4.
                        raise ValueError("invalid_worker_dimensions")
                    if output.width * output.height > MAX_DECODE_PIXELS:
                        raise ValueError("worker_output_too_large")
                    worker_alpha = settings.get("provider") != "fal" and ("A" in output.getbands() or "transparency" in output.info)
                    source = images.enter_context(output.convert("RGBA" if has_alpha and worker_alpha else "RGB"))
                if has_alpha and not worker_alpha:
                    with Image.open(io.BytesIO(data)) as original, ExitStack() as originals:
                        transposed = originals.enter_context(ImageOps.exif_transpose(original))
                        rgba = transposed if transposed.mode == "RGBA" else originals.enter_context(transposed.convert("RGBA"))
                        with rgba.getchannel("A") as alpha, alpha.resize(source.size, Image.Resampling.LANCZOS) as resized_alpha:
                            source.putalpha(resized_alpha)
            output_size = expected_size if mode == "super_resolution" and settings.get("provider") == "fal" else contained_dimensions(source.size, target)
            output = source if source.size == output_size else images.enter_context(source.resize(output_size, Image.Resampling.LANCZOS))
            with io.BytesIO() as buffer:
                output.save(buffer, format="PNG")
                metadata.update(actual_size=f"{output.width}x{output.height}", processing=mode, processing_status="applied")
                logger.info({"event": "image_calibration", **metadata, "duration_ms": round((time.monotonic() - started) * 1000)})
                return CalibratedImage(buffer.getvalue(), metadata)
    except Exception as exc:
        # A post-processing error must never enter the account retry/settlement path.
        if mode != "none":
            metadata.update(processing_status="fallback", processing_error="calibration_failed")
            logger.warning({"event": "image_calibration_failed", "mode": mode, "reason": type(exc).__name__, "duration_ms": round((time.monotonic() - started) * 1000)})
        return CalibratedImage(data, metadata)
