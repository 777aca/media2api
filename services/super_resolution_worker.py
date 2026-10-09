"""Standalone CPU worker. Run with the optional ``super-resolution`` dependencies."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import io
import os
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Request, Response
from PIL import Image, ImageOps

SCALE, TILE, PAD = 4, 256, 16
MAX_BYTES = 64 * 1024 * 1024
MAX_INPUT_PIXELS = 8_294_400


class Upscaler:
    def __init__(self, model_path: Path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(32, int(os.environ.get("SUPER_RESOLUTION_THREADS", "2"))))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name

    def upscale(self, data: bytes, deadline: float) -> bytes:
        import numpy as np

        with Image.open(io.BytesIO(data)) as opened:
            if opened.width * opened.height > MAX_INPUT_PIXELS:
                raise ValueError("image exceeds worker pixel limit")
            source = ImageOps.exif_transpose(opened).convert("RGBA" if "A" in opened.getbands() or "transparency" in opened.info else "RGB")
        width, height = source.size
        rgb = np.asarray(source.convert("RGB"), dtype=np.float32) / 255.0
        result = Image.new("RGB", (width * SCALE, height * SCALE))
        for y in range(0, height, TILE):
            for x in range(0, width, TILE):
                if time.monotonic() >= deadline:
                    raise TimeoutError("inference deadline exceeded")
                right, bottom = min(x + TILE, width), min(y + TILE, height)
                left_pad, top_pad = max(0, x - PAD), max(0, y - PAD)
                right_pad, bottom_pad = min(width, right + PAD), min(height, bottom + PAD)
                tensor = np.ascontiguousarray(rgb[top_pad:bottom_pad, left_pad:right_pad].transpose(2, 0, 1)[None])
                prediction = self.session.run(None, {self.input_name: tensor})[0]
                expected = (1, 3, (bottom_pad - top_pad) * SCALE, (right_pad - left_pad) * SCALE)
                if prediction.shape != expected or not np.isfinite(prediction).all():
                    raise ValueError("invalid model output")
                pixels = np.rint(np.clip(prediction[0].transpose(1, 2, 0), 0, 1) * 255).astype(np.uint8)
                tile = Image.fromarray(pixels).crop(((x-left_pad)*SCALE, (y-top_pad)*SCALE, (right-left_pad)*SCALE, (bottom-top_pad)*SCALE))
                result.paste(tile, (x * SCALE, y * SCALE))
        if source.mode == "RGBA":
            result.putalpha(source.getchannel("A").resize(result.size, Image.Resampling.LANCZOS))
        if time.monotonic() >= deadline:
            raise TimeoutError("inference deadline exceeded")
        output = io.BytesIO()
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
        if pending >= max_queue + 1:
            raise HTTPException(503, "queue full")
        pending += 1
        deadline = time.monotonic() + timeout_secs
        try:
            requested_timeout = request.headers.get("x-super-resolution-timeout")
            if requested_timeout and requested_timeout.isdecimal():
                deadline = min(deadline, time.monotonic() + max(1, int(requested_timeout)))
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                data = bytearray()
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise HTTPException(413, "image too large")
                async with gate:
                    # Shield the task so a disconnected/timed-out caller never releases
                    # the single inference slot while native ONNX code is still running.
                    task = asyncio.create_task(asyncio.to_thread(engine.upscale, bytes(data), deadline))
                    try:
                        result = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        try:
                            await task
                        except Exception:
                            pass
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
