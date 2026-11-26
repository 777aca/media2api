"""fal-ai/esrgan queue client. Submit each image once and keep billing metrics server-side."""
from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from PIL import Image, ImageOps

MODEL_URL = "https://queue.fal.run/fal-ai/esrgan"


@dataclass(frozen=True)
class FalResult:
    image: bytes
    request_id: str
    inference_seconds: float | None


def _input(data: bytes, target_size: tuple[int, int] | None) -> dict[str, object]:
    # Send an oriented RGB image. The calibration stage restores the original alpha.
    with Image.open(io.BytesIO(data)) as opened, ImageOps.exif_transpose(opened) as oriented:
        scale = min(4.0, target_size[0] / oriented.width, target_size[1] / oriented.height) if target_size else 4.0
        with oriented.convert("RGB") as rgb, io.BytesIO() as buffer:
            rgb.info.clear()
            rgb.save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getbuffer()).decode("ascii")
    return {
        "image_url": f"data:image/png;base64,{encoded}",
        "model": "RealESRGAN_x4plus",
        "scale": scale,
        "output_format": "png",
    }


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("fal_timeout")
    return seconds


def upscale(data: bytes, target_size: tuple[int, int] | None, timeout: int, max_bytes: int, *, api_key: str) -> FalResult:
    if not api_key:
        raise ValueError("fal_key_missing")
    deadline = time.monotonic() + timeout
    headers = {"Authorization": f"Key {api_key}", "X-Fal-Store-IO": "0"}
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        submitted = client.post(MODEL_URL, headers=headers, json=_input(data, target_size), timeout=_remaining(deadline))
        submitted.raise_for_status()
        request_id = str(UUID(submitted.json()["request_id"]))
        del submitted  # Release the base64 request body before waiting in the queue.
        job_url = f"{MODEL_URL}/requests/{request_id}"
        try:
            while True:
                status = client.get(f"{job_url}/status", headers=headers, timeout=_remaining(deadline))
                status.raise_for_status()
                job = status.json()
                if job["status"] == "COMPLETED":
                    break
                if job["status"] not in ("IN_QUEUE", "IN_PROGRESS"):
                    raise ValueError("fal_invalid_status")
                time.sleep(min(1.0, _remaining(deadline)))
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError):
            # A queued request can still be cancelled even if the local deadline elapsed.
            try:
                client.put(f"{job_url}/cancel", headers=headers, timeout=2)
            except httpx.HTTPError:
                pass
            raise
        if job.get("error") or job.get("error_type"):
            raise RuntimeError("fal_inference_failed")
        metrics = job.get("metrics")
        seconds = metrics.get("inference_time") if isinstance(metrics, dict) else None
        inference_seconds = float(seconds) if type(seconds) in (int, float) and seconds >= 0 else None
        result = client.get(job_url, headers=headers, timeout=_remaining(deadline))
        result.raise_for_status()
        url = result.json()["image"]["url"]
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or not (host == "fal.media" or host.endswith(".fal.media"))):
            raise ValueError("fal_invalid_image_url")
        with client.stream("GET", url, timeout=_remaining(deadline)) as response:
            response.raise_for_status()
            image = bytearray()
            for chunk in response.iter_bytes(64 * 1024):
                _remaining(deadline)
                if len(image) + len(chunk) > max_bytes:
                    raise ValueError("fal_output_too_large")
                image.extend(chunk)
            return FalResult(bytes(image), request_id, inference_seconds)
