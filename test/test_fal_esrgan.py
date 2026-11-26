import base64
import io
import json
import os
import unittest
from unittest import mock

import httpx
from PIL import Image

from services import fal_esrgan
from services.image_calibration import calibrate_image, worker_ready
from services.image_resolution import calibration_settings


JOB_ID = "00000000-0000-4000-8000-000000000001"
JOB_URL = f"{fal_esrgan.MODEL_URL}/requests/{JOB_ID}"
IMAGE_URL = "https://v3b.fal.media/files/test.png"
TEST_API_KEY = "test-saved-key"
SETTINGS = calibration_settings({"enabled": True, "provider": "fal", "fal_api_key": TEST_API_KEY})


def png(size, mode="RGB"):
    color = (30, 60, 90, 127) if mode == "RGBA" else (30, 60, 90)
    with Image.new(mode, size, color) as image, io.BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


class FalCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

    def queue(self, replies):
        replies = iter(replies)

        def handle(request):
            self.requests.append(request)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, bytes):
                return httpx.Response(200, content=reply)
            return httpx.Response(200, json=reply)

        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.enterContext(mock.patch.object(fal_esrgan.httpx, "Client", return_value=client))

    def test_portrait_through_calibration(self):
        self.queue([
            {"request_id": JOB_ID}, {"status": "IN_QUEUE"}, {"status": "IN_PROGRESS"},
            {"status": "COMPLETED", "metrics": {"inference_time": 1.25}},
            {"image": {"url": IMAGE_URL}}, png((2160, 3840)),
        ])
        with mock.patch.object(fal_esrgan.time, "sleep"), mock.patch("services.image_calibration.logger.info") as log:
            result = calibrate_image(png((576, 1024), "RGBA"), "2160x3840", SETTINGS)
        self.assertEqual(result.metadata["processing_status"], "applied")
        with Image.open(io.BytesIO(result.data)) as output:
            self.assertEqual(output.size, (2160, 3840))
            self.assertEqual(output.getpixel((100, 100))[3], 127)
        self.assertEqual([(r.method, str(r.url)) for r in self.requests], [
            ("POST", fal_esrgan.MODEL_URL), *(("GET", JOB_URL + "/status") for _ in range(3)),
            ("GET", JOB_URL), ("GET", IMAGE_URL),
        ])
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["scale"], 3.75)
        self.assertEqual(payload["model"], "RealESRGAN_x4plus")
        with Image.open(io.BytesIO(base64.b64decode(payload["image_url"].split(",")[1]))) as sent:
            self.assertEqual((sent.size, sent.mode), ((576, 1024), "RGB"))
        for request in self.requests[:-1]:
            self.assertEqual(request.headers["authorization"], f"Key {TEST_API_KEY}")
        self.assertNotIn("authorization", self.requests[-1].headers)
        log.assert_any_call({"event": "fal_esrgan_completed", "request_id": JOB_ID, "inference_time": 1.25})

    def test_timeout_cancels_and_keeps_original(self):
        self.queue([{"request_id": JOB_ID}, httpx.ReadTimeout("timed out"), {}])
        source = png((64, 64))
        result = calibrate_image(source, "256x256", SETTINGS)
        self.assertEqual(result.data, source)
        self.assertEqual(result.metadata["processing_status"], "fallback")
        self.assertEqual([r.method for r in self.requests], ["POST", "GET", "PUT"])
        self.assertEqual(str(self.requests[-1].url), JOB_URL + "/cancel")

    def test_failed_inference_does_not_resubmit(self):
        self.queue([{"request_id": JOB_ID}, {"status": "COMPLETED", "error": "failed"}])
        with self.assertRaisesRegex(RuntimeError, "fal_inference_failed"):
            fal_esrgan.upscale(png((32, 32)), None, 3, 1024, api_key=TEST_API_KEY)
        self.assertEqual([r.method for r in self.requests], ["POST", "GET"])

    def test_rejects_external_image_url(self):
        self.queue([{"request_id": JOB_ID}, {"status": "COMPLETED"}, {"image": {"url": "https://example.org/a.png"}}])
        with self.assertRaisesRegex(ValueError, "fal_invalid_image_url"):
            fal_esrgan.upscale(png((32, 32)), None, 3, 1024, api_key=TEST_API_KEY)
        self.assertEqual(len(self.requests), 3)

    def test_bounds_download_size(self):
        self.queue([{"request_id": JOB_ID}, {"status": "COMPLETED"}, {"image": {"url": IMAGE_URL}}, b"x" * 1025])
        with self.assertRaisesRegex(ValueError, "fal_output_too_large"):
            fal_esrgan.upscale(png((32, 32)), None, 3, 1024, api_key=TEST_API_KEY)

    def test_rejects_wrong_dimensions(self):
        self.queue([{"request_id": JOB_ID}, {"status": "COMPLETED"}, {"image": {"url": IMAGE_URL}}, png((20, 20))])
        source = png((16, 16))
        result = calibrate_image(source, "64x64", SETTINGS)
        self.assertEqual(result.data, source)
        self.assertEqual(result.metadata["processing_status"], "fallback")

    def test_settings_and_non_billable_checks(self):
        self.assertEqual(calibration_settings({"enabled": True})["provider"], "worker")
        with self.assertRaises(ValueError):
            calibration_settings({"provider": "unknown"})
        unconfigured = calibration_settings({"enabled": True, "provider": "fal"})
        with mock.patch.object(fal_esrgan.httpx, "Client") as client, mock.patch.dict(os.environ, {"FAL_KEY": "test-env-key"}):
            self.assertTrue(worker_ready(SETTINGS)["ok"])
            self.assertFalse(worker_ready(unconfigured)["ok"])
            source = png((128, 128))
            for target, processing in [("128x128", "none"), ("160x160", "resize")]:
                self.assertEqual(calibrate_image(source, target, unconfigured).metadata["processing"], processing)
            result = calibrate_image(source, "512x512", unconfigured)
            self.assertEqual(result.metadata["processing_status"], "fallback")
            self.assertEqual(result.data, source)
            client.assert_not_called()
