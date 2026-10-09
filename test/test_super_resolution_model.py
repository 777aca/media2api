"""Opt-in real CPU inference, using synthetic pixels and a local model only.

Set MEDIA2API_TEST_SR_MODEL to an absolute model path and run through the
isolated test runner. No upstream image generation or model download occurs.
"""
import io
import os
from pathlib import Path
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from services.image_calibration import calibrate_image
from services.image_resolution import DEFAULT_CALIBRATION
from services.super_resolution_worker import Upscaler, create_worker


@unittest.skipUnless(os.environ.get("MEDIA2API_TEST_SR_MODEL"), "local model path required")
class RealSuperResolutionTests(unittest.TestCase):
    def test_real_model_tiles_alpha_and_2k_4k_calibration(self):
        import numpy as np

        output_dir = Path(os.environ["MEDIA2API_TEST_SR_OUTPUT"])
        output_dir.mkdir(parents=True, exist_ok=True)
        pixels = np.zeros((288, 512, 4), dtype=np.uint8)
        pixels[:, :, 0] = np.linspace(20, 220, 512).astype(np.uint8)[None, :]
        pixels[:, :, 1] = np.linspace(30, 200, 288).astype(np.uint8)[:, None]
        pixels[:, :, 2] = 100
        pixels[:, :, 3] = np.linspace(0, 255, 512).astype(np.uint8)[None, :]
        image = Image.fromarray(pixels)
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        source = buffer.getvalue()
        (output_dir / "synthetic-source.png").write_bytes(source)
        started = time.monotonic()
        engine = Upscaler(Path(os.environ["MEDIA2API_TEST_SR_MODEL"]))
        with TestClient(create_worker(upscaler=engine, secret="local-test")) as client:
            response = client.post("/v1/super-resolution", content=source, headers={"x-super-resolution-secret": "local-test"})
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else "")
        with Image.open(io.BytesIO(response.content)) as result:
            self.assertEqual(result.size, (2048, 1152))
            self.assertEqual(result.getchannel("A").getextrema(), (0, 255))
            rgb = np.asarray(result.convert("RGB"), dtype=np.float32)
            # Smooth input has no edge at tile boundaries: seam deltas must stay small.
            for left, right in [(rgb[:, 1023], rgb[:, 1024]), (rgb[1023, :], rgb[1024, :])]:
                self.assertLess(float(np.mean(np.abs(left - right))), 5.0)
        for size in ["2048x1152", "3840x2160"]:
            with mock.patch("services.image_calibration.super_resolve", return_value=response.content):
                result = calibrate_image(source, size, {**DEFAULT_CALIBRATION, "enabled": True})
            self.assertEqual(result.metadata["actual_size"], size)
            self.assertEqual(result.metadata["processing"], "super_resolution")
            (output_dir / f"synthetic-{size}.png").write_bytes(result.data)
        print(f"Real ONNX model inference and 2K/4K calibration: {time.monotonic()-started:.2f}s; tiles/alpha verified")
