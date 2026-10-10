"""Standalone CPU worker. Run with the optional ``super-resolution`` dependencies."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, ExitStack
import hmac
import io
import os
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Request, Response
from PIL import Image, ImageOps

from services.image_resolution import ImageSizeError, contained_dimensions, validate_image_size, parse_dimensions

SCALE, TILE, PAD = 4, 256, 16
MAX_BYTES = 64 * 1024 * 1024
MAX_INPUT_PIXELS = 8_294_400
MAX_OUTPUT_PIXELS = 40_000_000


class Upscaler:
    def __init__(self, model_path: Path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(32, int(os.environ.get("SUPER_RESOLUTION_THREADS", "2"))))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name

    def upscale(self, data: bytes, deadline: float, *, target_size: tuple[int, int] | None = None) -> bytes:
        import numpy as np

        with ExitStack() as images:
            with Image.open(io.BytesIO(data)) as opened:
                if opened.width * opened.height > MAX_INPUT_PIXELS:
                    raise ValueError("image exceeds worker pixel limit")
                if target_size is None and opened.width * opened.height * SCALE ** 2 > MAX_OUTPUT_PIXELS:
                    raise ValueError("worker output exceeds pixel limit")
                transposed = images.enter_context(ImageOps.exif_transpose(opened))
                mode = "RGBA" if "A" in opened.getbands() or "transparency" in opened.info else "RGB"
                source = transposed if transposed.mode == mode else images.enter_context(transposed.convert(mode))
            width, height = source.size
            full_size = (width * SCALE, height * SCALE)
            output_size = contained_dimensions(full_size, target_size) if target_size else full_size
            if output_size[0] * output_size[1] > MAX_OUTPUT_PIXELS:
                raise ValueError("worker output exceeds pixel limit")
            alpha = images.enter_context(source.getchannel("A")) if source.mode == "RGBA" else None
            result = images.enter_context(Image.new(source.mode, output_size))
            for y in range(0, height, TILE):
                for x in range(0, width, TILE):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("inference deadline exceeded")
                    right, bottom = min(x + TILE, width), min(y + TILE, height)
                    left_pad, top_pad = max(0, x - PAD), max(0, y - PAD)
                    right_pad, bottom_pad = min(width, right + PAD), min(height, bottom + PAD)
                    destination = (round(x * output_size[0] / width), round(y * output_size[1] / height),
                                   round(right * output_size[0] / width), round(bottom * output_size[1] / height))
                    if destination[2] <= destination[0] or destination[3] <= destination[1]:
                        continue
                    # Only the padded tile becomes float32; queued images and the
                    # source no longer require a full-image floating point array.
                    with source.crop((left_pad, top_pad, right_pad, bottom_pad)) as region, region.convert("RGB") as rgb:
                        pixels = np.asarray(rgb, dtype=np.float32)
                        pixels /= 255.0
                        tensor = np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])
                    prediction = self.session.run(None, {self.input_name: tensor})[0]
                    expected = (1, 3, (bottom_pad - top_pad) * SCALE, (right_pad - left_pad) * SCALE)
                    if prediction.shape != expected or not np.isfinite(prediction).all():
                        raise ValueError("invalid model output")
                    np.clip(prediction, 0, 1, out=prediction)
                    prediction *= 255
                    np.rint(prediction, out=prediction)
                    pixels = prediction[0].transpose(1, 2, 0).astype(np.uint8)
                    # Map the global resize coordinates into this padded tile.
                    # The overlap supplies Lanczos samples across tile boundaries.
                    box = (destination[0] * full_size[0] / output_size[0] - left_pad * SCALE,
                           destination[1] * full_size[1] / output_size[1] - top_pad * SCALE,
                           destination[2] * full_size[0] / output_size[0] - left_pad * SCALE,
                           destination[3] * full_size[1] / output_size[1] - top_pad * SCALE)
                    with Image.fromarray(pixels) as tile:
                        if alpha is not None:
                            with alpha.crop((left_pad, top_pad, right_pad, bottom_pad)) as region_alpha, region_alpha.resize(tile.size, Image.Resampling.LANCZOS) as tile_alpha:
                                tile.putalpha(tile_alpha)
                        # RGBA resizing premultiplies alpha, preserving transparent
                        # edges. An exact x4 crop avoids needless color rounding.
                        with (tile.crop(tuple(round(value) for value in box)) if output_size == full_size else tile.resize(
                            (destination[2] - destination[0], destination[3] - destination[1]),
                            Image.Resampling.LANCZOS, box=box,
                        )) as resized:
                            result.paste(resized, destination[:2])
                    del pixels, prediction, tensor
            if time.monotonic() >= deadline:
                raise TimeoutError("inference deadline exceeded")
            with io.BytesIO() as output:
                result.save(output, format="PNG")
                if output.tell() > MAX_BYTES:
                    raise ValueError("worker output exceeds byte limit")
                return output.getvalue()


def create_worker(*, upscaler: Upscaler | None = None, secret: str | None = None, timeout_secs: int = 300, max_queue: int = 8) -> FastAPI:
    worker_secret = secret if secret is not None else os.environ.get("SUPER_RESOLUTION_WORKER_SECRET", "")
    engine = upscaler
    gate = asyncio.Semaphore(1)
    pending = 0

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal engine
        if engine is None:
            model_path = Path(os.environ.get("REALESR_MODEL_PATH", "data/models/realesr-general-x4v3.onnx"))
            try:
                engine = await asyncio.to_thread(Upscaler, model_path)
            except Exception:
                # Keep health endpoint reachable; no implicit model downloads.
                engine = None
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(request: Request) -> None:
        if worker_secret and not hmac.compare_digest(request.headers.get("x-super-resolution-secret", "").encode(), worker_secret.encode()):
            raise HTTPException(401, "unauthorized")

    @app.get("/health/ready")
    async def ready(request: Request):
        authorize(request)
        if engine is None:
            raise HTTPException(503, "model unavailable")
        return {"ok": True, "scale": SCALE}

    @app.post("/v1/super-resolution")
    async def upscale(request: Request):
        nonlocal pending
        authorize(request)
        if engine is None:
            raise HTTPException(503, "model unavailable")
        requested_size = request.headers.get("x-super-resolution-size")
        target_size = None
        if requested_size is not None:
            try:
                target_size = parse_dimensions(validate_image_size(requested_size))
                if target_size is None:
                    raise ImageSizeError("explicit size required")
            except ImageSizeError:
                raise HTTPException(400, "invalid output size") from None
        if pending >= max_queue + 1:
            raise HTTPException(503, "queue full")
        pending += 1
        deadline = time.monotonic() + timeout_secs
        try:
            requested_timeout = request.headers.get("x-super-resolution-timeout")
            if requested_timeout and requested_timeout.isdecimal():
                deadline = min(deadline, time.monotonic() + max(1, int(requested_timeout)))
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                async with gate:
                    # Apply ASGI backpressure while waiting instead of buffering
                    # up to 64 MiB for every queued request in Python memory.
                    data = bytearray()
                    async for chunk in request.stream():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise HTTPException(413, "image too large")
                    payload = bytes(data)
                    del data
                    # Shield the task so a disconnected/timed-out caller never releases
                    # the single inference slot while native ONNX code is still running.
                    options = {"target_size": target_size} if target_size else {}
                    task = asyncio.create_task(asyncio.to_thread(engine.upscale, payload, deadline, **options))
                    try:
                        result = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        while not task.done():
                            try:
                                await asyncio.shield(task)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        if not task.cancelled():
                            task.exception()
                        raise
                return Response(result, media_type="image/png")
        except TimeoutError:
            raise HTTPException(504, "super-resolution timed out") from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, "invalid image or model output") from None
        finally:
            pending -= 1

    return app


app = create_worker(timeout_secs=max(1, min(600, int(os.environ.get("SUPER_RESOLUTION_TIMEOUT", "300")))))
