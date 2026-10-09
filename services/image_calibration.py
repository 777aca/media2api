"""Calibrate final image bytes once, before storage and API serialization."""
from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass

from curl_cffi import requests
from PIL import Image, ImageOps

from services.image_resolution import parse_dimensions
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
    try:
        with requests.Session(trust_env=False) as session:
            response = session.get(f"{settings['worker_url']}/health/ready", headers=_headers(), timeout=5, allow_redirects=False)
            try:
                if response.status_code == 200:
                    return {"ok": True, "message": "超分服务和模型已就绪"}
                return {"ok": False, "message": f"超分服务未就绪（HTTP {response.status_code}）"}
            finally:
                response.close()
    except Exception:
        return {"ok": False, "message": "无法连接超分服务，请检查地址、鉴权和模型"}


def super_resolve(data: bytes, settings: dict[str, object]) -> bytes:
    if len(data) > MAX_BODY_BYTES:
        raise ValueError("image_too_large")
    with requests.Session(trust_env=False) as session:
        response = session.post(
            f"{settings['worker_url']}/v1/super-resolution", data=data,
            headers={**_headers(), "Content-Type": "application/octet-stream", "x-super-resolution-timeout": str(settings["timeout_secs"])},
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
        with Image.open(io.BytesIO(data)) as opened:
            # Metadata only on the disabled path; never re-encode upstream bytes.
            actual = opened.size
            metadata.update(source_size=f"{actual[0]}x{actual[1]}", actual_size=f"{actual[0]}x{actual[1]}")
            target = parse_dimensions(requested_size)
            mode = calibration_mode(actual, target) if settings["enabled"] else "none"
            if mode == "none" or target is None:
                return CalibratedImage(data, metadata)
            if len(data) > MAX_BODY_BYTES or actual[0] * actual[1] > MAX_DECODE_PIXELS:
                raise ValueError("image_too_large")
            source = ImageOps.exif_transpose(opened).convert("RGBA" if "A" in opened.getbands() or "transparency" in opened.info else "RGB")
        if mode == "super_resolution":
            upscaled = super_resolve(data, settings)
            with Image.open(io.BytesIO(upscaled)) as output:
                if output.size != (source.width * 4, source.height * 4):
                    raise ValueError("invalid_worker_dimensions")
                enhanced = output.convert("RGB")
            if source.mode == "RGBA":
                enhanced.putalpha(source.getchannel("A").resize(enhanced.size, Image.Resampling.LANCZOS))
            source = enhanced
        # Contain allows enlargement, preserves the composition, and never crops.
        output = ImageOps.contain(source, target, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
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
