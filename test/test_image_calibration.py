from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from services.config import config
from services.image_calibration import calibrate_image, calibration_mode, worker_ready
from services.image_resolution import DEFAULT_CALIBRATION, ImageSizeError, calibration_settings, validate_image_size
from test.legacy_image_task_fixture import ImageTaskService
from services.protocol import conversation
from services.super_resolution_worker import create_worker
from utils.image_tokens import count_image_output_items_tokens


def png(size=(512, 512), mode="RGBA") -> bytes:
    stream = io.BytesIO()
    Image.new(mode, size, (45, 90, 150, 127) if mode == "RGBA" else (45, 90, 150)).save(stream, format="PNG")
    return stream.getvalue()


class ImageResolutionTests(unittest.TestCase):
    def test_official_sizes_and_boundaries(self):
        for size in [None, "auto", "1024x640", "2560x1440", "2048x1152", "3840x2160", "2160x3840", "2880x2880", "1536x512"]:
            with self.subTest(size=size):
                self.assertEqual(validate_image_size(size), size)
        self.assertEqual(validate_image_size(" 2048X2048 "), "2048x2048")

    def test_invalid_sizes_fail_before_any_account_selection(self):
        for size in [True, 2048, "NaNx1024", "1365x1024", "3840x3840", "3856x1024", "1024x512", "3072x768", "0x1024", "1024x1024 trailing"]:
            with self.subTest(size=size), mock.patch.object(conversation.account_service, "get_available_access_token") as account:
                with self.assertRaises(ImageSizeError):
                    conversation.ConversationRequest(size=size)
                account.assert_not_called()

    def test_threshold_and_rotated_sizes(self):
        self.assertEqual(calibration_mode((2559, 1440), (3840, 2160)), "super_resolution")
        self.assertEqual(calibration_mode((2560, 1440), (3840, 2160)), "resize")
        self.assertEqual(calibration_mode((2160, 3840), (3840, 2160)), "none")
        self.assertEqual(calibration_mode((512, 512), None), "none")

    def test_settings_are_validated(self):
        self.assertEqual(calibration_settings(None), DEFAULT_CALIBRATION)
        for fields in [{"enabled": "yes"}, {"worker_url": "file:///tmp/model"}, {"worker_url": "http://user:secret@localhost"}, {"timeout_secs": 0}, {"timeout_secs": True}, {"timeout_secs": 601}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                calibration_settings(fields)


class ImageCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.settings = {**DEFAULT_CALIBRATION, "enabled": True}

    def test_disabled_auto_and_already_large_preserve_bytes(self):
        source = png()
        with mock.patch("services.image_calibration.super_resolve") as worker:
            for size, enabled in [("2048x2048", False), ("auto", True), (None, True), ("256x256", True)]:
                result = calibrate_image(source, size, {**self.settings, "enabled": enabled})
                self.assertEqual(result.data, source)
                self.assertEqual(result.metadata["processing"], "none")
            worker.assert_not_called()

    def test_resize_preserves_aspect_and_alpha(self):
        result = calibrate_image(png((1024, 768)), "1536x864", self.settings)
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual(image.size, (1152, 864))
            self.assertEqual(image.getchannel("A").getextrema(), (127, 127))
        self.assertEqual(result.metadata["processing"], "resize")
        self.assertEqual(result.metadata["source_size"], "1024x768")

    def test_super_resolution_uses_four_times_result_and_keeps_alpha(self):
        with mock.patch("services.image_calibration.super_resolve", return_value=png((2048, 2048), "RGB")) as worker:
            result = calibrate_image(png(), "2048x1152", self.settings)
        worker.assert_called_once()
        self.assertEqual(result.metadata["actual_size"], "1152x1152")
        self.assertEqual(result.metadata["processing"], "super_resolution")
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual(image.getchannel("A").getextrema(), (127, 127))

    def test_worker_errors_and_invalid_outputs_fall_back(self):
        source = png()
        for exception in [TimeoutError(), ConnectionError(), ValueError()]:
            with self.subTest(error=type(exception)), mock.patch("services.image_calibration.super_resolve", side_effect=exception):
                result = calibrate_image(source, "2048x2048", self.settings)
                self.assertEqual(result.data, source)
                self.assertEqual(result.metadata["processing_status"], "fallback")
        for invalid in [b"not an image", png((32, 32))]:
            with mock.patch("services.image_calibration.super_resolve", return_value=invalid):
                self.assertEqual(calibrate_image(source, "2048x2048", self.settings).data, source)

    def test_format_multi_image_url_base64_and_usage_are_consistent(self):
        source = png((1024, 768))
        raw_items = [{"b64_json": base64.b64encode(source).decode()}] * 2
        stored = []
        def save(data, _base):
            stored.append(data)
            return f"http://test/images/{len(stored)}.png"
        with mock.patch.object(config, "get_image_calibration_settings", return_value=self.settings), mock.patch.object(conversation, "save_image_bytes", side_effect=save):
            outputs = conversation.format_image_result(raw_items, "synthetic", "b64_json", requested_size="1536x864")["data"]
            url_outputs = conversation.format_image_result(raw_items, "synthetic", "url", requested_size="1536x864")["data"]
        self.assertEqual(len(outputs), 2)
        for index, item in enumerate(outputs):
            self.assertEqual(base64.b64decode(item["b64_json"]), stored[index])
            self.assertEqual(item["actual_size"], "1152x864")
        self.assertNotIn("b64_json", url_outputs[0])
        expected = count_image_output_items_tokens(raw_items, "1536x864")
        self.assertEqual(count_image_output_items_tokens(outputs, "1536x864"), expected)
        self.assertEqual(count_image_output_items_tokens(url_outputs, "1536x864"), expected)
        from services.protocol.openai_v1_response import image_output_items
        self.assertEqual(image_output_items("synthetic", outputs)[0]["processing"], "resize")

    def test_unavailable_health_does_not_leak_exception_details(self):
        with mock.patch("services.image_calibration.requests.Session.get", side_effect=RuntimeError("secret")):
            status = worker_ready(self.settings)
        self.assertFalse(status["ok"])
        self.assertNotIn("secret", status["message"])

    def test_task_rejects_invalid_size_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImageTaskService(path=Path(directory) / "tasks.json")
            with mock.patch.object(service, "_run_task") as run:
                with self.assertRaises(ImageSizeError):
                    service.submit_generation({"id": "test", "role": "admin"}, client_task_id="invalid", prompt="synthetic", model="gpt-image-2", size="1365x1024", base_url="http://test")
                run.assert_not_called()

    def test_recovered_task_uses_original_target_and_persists_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImageTaskService(Path(directory) / "tasks.json")
            key = "test:recovered"
            service._tasks[key] = {"id": "recovered", "owner_id": "test", "pool_account_id": "pool-original", "size": "1536x864", "quality": "auto", "status": "running"}
            backend = mock.Mock()
            backend._poll_image_results.return_value = (["synthetic-file"], [])
            backend.resolve_conversation_image_urls.return_value = ["http://test/image"]
            backend.download_image_bytes.return_value = [png((1024, 768))]
            with mock.patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend) as create_backend, mock.patch("test.legacy_image_task_fixture.account_service.list_accounts", return_value=[{"pool_account_id": "pool-original", "access_token": "test-token"}]), mock.patch.object(config, "get_image_calibration_settings", return_value=self.settings), mock.patch.object(conversation, "save_image_bytes", return_value="http://test/saved"), mock.patch.object(service, "_log_call"):
                service._run_resume_poll(key, "synthetic-conversation", 30, {"id": "test", "role": "admin"}, "generate", "gpt-image-2")
            create_backend.assert_called_once_with(access_token="test-token")
            output = service._tasks[key]
            self.assertEqual(output["status"], "success", output.get("error"))
            self.assertEqual(output["data"][0]["actual_size"], "1152x864")
            self.assertEqual(output["data"][0]["requested_size"], "1536x864")
            reloaded = ImageTaskService(service.path)
            self.assertEqual(reloaded._tasks[key]["data"][0]["processing"], "resize")
            backend.stream_conversation.assert_not_called()

    def test_config_save_and_failed_save_preserve_existing_settings(self):
        previous = config.data
        self.addCleanup(setattr, config, "data", previous)
        config.data = dict(previous)
        with mock.patch.object(config, "_save"):
            result = config.update({"image_calibration": self.settings})
        self.assertEqual(result["image_calibration"], self.settings)
        with mock.patch.object(config, "_save", side_effect=OSError("test")):
            with self.assertRaises(OSError):
                config.update({"image_calibration": DEFAULT_CALIBRATION})
        self.assertEqual(config.get_image_calibration_settings(), self.settings)


class ImageCalibrationAPITests(unittest.TestCase):
    def test_invalid_generation_size_returns_400_without_upstream(self):
        from api.ai import create_router
        app = FastAPI()
        app.include_router(create_router())
        with mock.patch("api.ai.require_identity", return_value={"id": "test", "role": "admin"}), mock.patch("api.ai.check_request"), mock.patch.object(conversation.account_service, "get_available_access_token") as account:
            client = TestClient(app)
            for streaming in [False, True]:
                response = client.post("/v1/images/generations", json={"prompt": "test", "size": "1365x1024", "stream": streaming})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["param"], "size")
            account.assert_not_called()

    def test_health_endpoint_requires_admin_and_tests_unsaved_settings(self):
        from api.system import create_router
        app = FastAPI()
        app.include_router(create_router("test"))
        client = TestClient(app)
        self.assertEqual(client.post("/api/image-calibration/test", json=DEFAULT_CALIBRATION).status_code, 401)
        with mock.patch("api.system.require_admin"), mock.patch("api.system.worker_ready", return_value={"ok": True, "message": "ready"}) as ready:
            response = client.post("/api/image-calibration/test", json={**DEFAULT_CALIBRATION, "worker_url": "http://candidate:3310"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(ready.call_args.args[0]["worker_url"], "http://candidate:3310")


class WorkerTests(unittest.TestCase):
    def test_authenticated_ready_and_inference(self):
        engine = mock.Mock()
        engine.upscale.return_value = png((32, 32))
        with TestClient(create_worker(upscaler=engine, secret="test-secret")) as client:
            self.assertEqual(client.get("/health/ready").status_code, 401)
            headers = {"x-super-resolution-secret": "test-secret"}
            self.assertEqual(client.get("/health/ready", headers=headers).status_code, 200)
            result = client.post("/v1/super-resolution", content=png((8, 8)), headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(Image.open(io.BytesIO(result.content)).size, (32, 32))

    def test_worker_missing_model_timeout_and_invalid_input(self):
        with mock.patch("services.super_resolution_worker.Upscaler", side_effect=FileNotFoundError()):
            with TestClient(create_worker(secret="")) as client:
                self.assertEqual(client.get("/health/ready").status_code, 503)
        for error, status in [(TimeoutError(), 504), (ValueError(), 422)]:
            engine = mock.Mock()
            engine.upscale.side_effect = error
            with TestClient(create_worker(upscaler=engine, secret="")) as client:
                self.assertEqual(client.post("/v1/super-resolution", content=b"bad").status_code, status)

    def test_queue_is_bounded(self):
        import httpx
        import threading
        gate = threading.Event()
        entered = threading.Event()
        engine = mock.Mock()
        def slow(_data, _deadline):
            entered.set()
            gate.wait(5)
            return png((16, 16))
        engine.upscale.side_effect = slow
        app = create_worker(upscaler=engine, secret="", max_queue=0)
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.post("/v1/super-resolution", content=png((4, 4))))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    second = await client.post("/v1/super-resolution", content=png((4, 4)))
                    self.assertEqual(second.status_code, 503)
                finally:
                    gate.set()
                self.assertEqual((await first).status_code, 200)
        asyncio.run(exercise())
