from __future__ import annotations

import asyncio
import importlib.util
import io
import threading
import time
import unittest
from unittest import mock

import httpx
from PIL import Image, ImageOps, PngImagePlugin

from services.image_calibration import calibrate_image, super_resolve
from services.image_resolution import DEFAULT_CALIBRATION, contained_dimensions
from services.super_resolution_worker import PAD, SCALE, TILE, Upscaler, create_worker


def png(size=(8, 8), mode="RGB"):
    with io.BytesIO() as buffer, Image.new(mode, size, (50, 100, 150, 127) if mode == "RGBA" else (50, 100, 150)) as image:
        image.save(buffer, "PNG")
        return buffer.getvalue()


class CalibrationMemoryTests(unittest.TestCase):
    def test_queue_wait_does_not_decode_source_and_target_result_preserves_alpha(self):
        data = png((512, 512), "RGBA")
        enhanced = png((1152, 1152))
        decoded = []
        original_load = PngImagePlugin.PngImageFile.load

        def load(image, *args, **kwargs):
            decoded.append(image.size)
            return original_load(image, *args, **kwargs)

        def resolve(_data, _settings, *, target_size):
            self.assertEqual(decoded, [])
            self.assertEqual(target_size, (2048, 1152))
            return enhanced

        with mock.patch.object(PngImagePlugin.PngImageFile, "load", load), mock.patch(
            "services.image_calibration.super_resolve", side_effect=resolve,
        ):
            result = calibrate_image(data, "2048x1152", {**DEFAULT_CALIBRATION, "enabled": True})
        self.assertEqual(result.metadata["processing_status"], "applied")
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual(image.size, (1152, 1152))
            self.assertEqual(image.getchannel("A").getextrema(), (127, 127))

    def test_transport_sends_target_and_closes_stream(self):
        response = mock.Mock(status_code=200)
        response.iter_content.return_value = [b"synthetic"]
        session = mock.MagicMock()
        session.__enter__.return_value.post.return_value = response
        with mock.patch("services.image_calibration.requests.Session", return_value=session):
            self.assertEqual(super_resolve(b"input", DEFAULT_CALIBRATION, target_size=(3840, 2160)), b"synthetic")
        self.assertEqual(session.__enter__.return_value.post.call_args.kwargs["headers"]["x-super-resolution-size"], "3840x2160")
        response.close.assert_called_once()

    def test_target_dimensions_follow_exif_orientation(self):
        with io.BytesIO() as buffer, Image.new("RGB", (12, 8), "red") as source:
            exif = Image.Exif()
            exif[274] = 6
            source.save(buffer, "PNG", exif=exif)
            data = buffer.getvalue()
        with mock.patch("services.image_calibration.super_resolve", return_value=png((768, 1152))):
            result = calibrate_image(data, "2048x1152", {**DEFAULT_CALIBRATION, "enabled": True})
        self.assertEqual(result.metadata["processing_status"], "applied")
        self.assertEqual(result.metadata["actual_size"], "768x1152")

    def test_oversized_legacy_output_falls_back_before_decoding(self):
        data = png((512, 512))
        enhanced = png((2048, 2048))
        with mock.patch("services.image_calibration.super_resolve", return_value=enhanced), mock.patch(
            "services.image_calibration.MAX_DECODE_PIXELS", 1_000_000,
        ):
            result = calibrate_image(data, "2048x1152", {**DEFAULT_CALIBRATION, "enabled": True})
        self.assertEqual(result.metadata["processing_status"], "fallback")
        self.assertEqual(result.data, data)


class WorkerQueueMemoryTests(unittest.TestCase):
    def test_queued_body_waits_and_repeated_cancellation_keeps_native_slot(self):
        release, entered = threading.Event(), threading.Event()
        body_read = asyncio.Event()
        calls = []
        engine = mock.Mock()

        def infer(_data, _deadline):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
            return png((32, 32))

        engine.upscale.side_effect = infer
        app = create_worker(upscaler=engine, secret="", max_queue=1)

        async def body():
            body_read.set()
            yield png()

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.post("/v1/super-resolution", content=png()))
                second = None
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    second = asyncio.create_task(client.post("/v1/super-resolution", content=body()))
                    await asyncio.sleep(.03)
                    self.assertFalse(body_read.is_set())
                    first.cancel()
                    await asyncio.sleep(.03)
                    first.cancel()
                    await asyncio.sleep(.03)
                    self.assertFalse(body_read.is_set())
                    self.assertEqual(len(calls), 1)
                    full = await client.post("/v1/super-resolution", content=body())
                    self.assertEqual(full.status_code, 503)
                    self.assertFalse(body_read.is_set())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await first
                    if second is not None:
                        self.assertEqual((await second).status_code, 200)
                self.assertTrue(body_read.is_set())
                self.assertEqual(len(calls), 2)

        asyncio.run(exercise())

    def test_waiting_timeout_never_reads_body_or_starts_inference(self):
        entered, release = threading.Event(), threading.Event()
        read = []
        engine = mock.Mock()

        def infer(_data, _deadline):
            entered.set()
            release.wait(5)
            return png((32, 32))

        engine.upscale.side_effect = infer
        app = create_worker(upscaler=engine, secret="", timeout_secs=1, max_queue=1)

        async def body():
            read.append(1)
            yield png()

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.post("/v1/super-resolution", content=png()))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    queued = await client.post("/v1/super-resolution", content=body())
                    self.assertEqual(queued.status_code, 504)
                    self.assertEqual(read, [])
                    self.assertEqual(engine.upscale.call_count, 1)
                finally:
                    release.set()
                    self.assertEqual((await first).status_code, 504)

        asyncio.run(exercise())

    def test_size_header_validates_and_reaches_engine(self):
        from fastapi.testclient import TestClient
        engine = mock.Mock()
        engine.upscale.return_value = png((2048, 1152))
        with TestClient(create_worker(upscaler=engine, secret="")) as client:
            for size in ["auto", "0x0", "9999x9999", "garbage"]:
                response = client.post("/v1/super-resolution", content=png(), headers={"x-super-resolution-size": size})
                self.assertEqual(response.status_code, 400)
            engine.upscale.assert_not_called()
            response = client.post("/v1/super-resolution", content=png(), headers={"x-super-resolution-size": "2048x1152"})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(engine.upscale.call_args.kwargs["target_size"], (2048, 1152))


@unittest.skipUnless(importlib.util.find_spec("numpy"), "optional numpy dependency required")
class TiledOutputTests(unittest.TestCase):
    def engine(self):
        import numpy as np
        engine = Upscaler.__new__(Upscaler)
        engine.input_name = "input"
        engine.session = mock.Mock()
        engine.session.run.side_effect = lambda _outputs, inputs: [np.repeat(np.repeat(inputs["input"], SCALE, axis=2), SCALE, axis=3)]
        return engine

    def test_tiles_compose_target_canvas_without_full_float_array_or_x4_canvas(self):
        import numpy as np
        pixels = np.zeros((340, 600, 4), dtype=np.uint8)
        pixels[:, :, 0] = np.linspace(10, 230, 600).astype(np.uint8)
        pixels[:, :, 1] = np.linspace(20, 210, 340).astype(np.uint8)[:, None]
        pixels[:, :, 2] = 100
        # Include sharp texture near both tile boundaries, as well as gradients.
        pixels[245:270, 245:270, :3] = np.random.default_rng(0).integers(0, 256, (25, 25, 3), dtype=np.uint8)
        pixels[:, :, 3] = np.linspace(0, 255, 600).astype(np.uint8)
        with Image.fromarray(pixels) as image, io.BytesIO() as buffer:
            image.save(buffer, "PNG")
            data = buffer.getvalue()
            with image.convert("RGB") as rgb, rgb.resize((2400, 1360), Image.Resampling.NEAREST) as full:
                with image.getchannel("A") as alpha, alpha.resize(full.size, Image.Resampling.LANCZOS) as full_alpha:
                    full.putalpha(full_alpha)
                reference = ImageOps.contain(full, (1051, 701), Image.Resampling.LANCZOS)
        float_sizes = []
        asarray = np.asarray

        def array(source, *args, **kwargs):
            if kwargs.get("dtype") == np.float32 and isinstance(source, Image.Image):
                float_sizes.append(source.size)
            return asarray(source, *args, **kwargs)

        with mock.patch.object(np, "asarray", side_effect=array), mock.patch.object(Image, "new", wraps=Image.new) as allocate:
            result = self.engine().upscale(data, time.monotonic() + 10, target_size=(1051, 701))
        self.assertGreater(len(float_sizes), 1)
        self.assertTrue(all(max(size) <= TILE + 2 * PAD for size in float_sizes))
        sizes = [call.args[1] for call in allocate.call_args_list]
        self.assertNotIn((2400, 1360), sizes)
        self.assertIn(contained_dimensions((2400, 1360), (1051, 701)), sizes)
        with reference, Image.open(io.BytesIO(result)) as output:
            self.assertEqual(output.size, reference.size)
            difference = np.abs(np.asarray(output.convert("RGB"), dtype=np.int16) - np.asarray(reference.convert("RGB"), dtype=np.int16))
            self.assertLess(float(difference.mean()), .1)
            self.assertLessEqual(int(difference.max()), 2)
            self.assertTrue(np.array_equal(np.asarray(output.getchannel("A")), np.asarray(reference.getchannel("A"))))

    def test_legacy_x4_dimensions_and_pixel_budget(self):
        engine = self.engine()
        result = engine.upscale(png(), time.monotonic() + 10)
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (32, 32))
        engine.session.reset_mock()
        with mock.patch("services.super_resolution_worker.MAX_OUTPUT_PIXELS", 100):
            with self.assertRaisesRegex(ValueError, "pixel limit"):
                engine.upscale(png(), time.monotonic() + 10)
        engine.session.run.assert_not_called()
