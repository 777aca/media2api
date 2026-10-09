from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile

import api.ai as ai
import api.image_tasks as image_tasks
from services.editable_file_task_service import EditableFileTaskService
from test.legacy_image_task_fixture import ImageTaskService
from services.log_service import LoggedCall, LogService
from services.request_log import MAX_TOTAL_CHARS, REDACTED, TRUNCATED, sanitize_request_parameters

IDENTITY = {"id": "test-admin", "role": "admin", "name": "Test"}
PNG_BYTES = b"\x89PNG\r\n\x1a\n"
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


class RequestParameterSanitizerTests(unittest.TestCase):
    def test_preserves_values_and_snapshots_nested_parameters(self):
        original = {"prompt": "line one\n" + "文字" * 900, "n": 2, "size": None, "stream": False,
                    "messages": [{"role": "user", "content": "cat"}], "max_tokens": 123}
        snapshot = sanitize_request_parameters(original)
        self.assertEqual(snapshot, original)
        original["messages"][0]["content"] = "mutated"
        self.assertEqual(snapshot["messages"][0]["content"], "cat")

    def test_redacts_credentials_recursively_and_signed_urls(self):
        parameters = {"api_key": "secret-a", "metadata": {"refreshToken": "secret-b", "password": "secret-c"},
                      "headers": {"Authorization": "Bearer secret-d", "Cookie": "secret-e"},
                      "image_url": "https://user:secret-f@example.test/a.png?w=512&token=secret-g&X-Amz-Signature=secret-h#secret-i"}
        snapshot = sanitize_request_parameters(parameters)
        text = json.dumps(snapshot)
        for suffix in "abcdefghi":
            self.assertNotIn(f"secret-{suffix}", text)
        self.assertEqual(snapshot["api_key"], REDACTED)
        self.assertIn("w=512", snapshot["image_url"])
        self.assertIn("example.test/a.png", snapshot["image_url"])

    def test_files_data_urls_and_raw_base64_are_metadata_only(self):
        upload = UploadFile(io.BytesIO(b"private-image"), filename="reference.png", size=13,
                            headers=Headers({"content-type": "image/png"}))
        parameters = {"image": upload, "mask": (b"private-mask", "mask.png", "image/png"),
                      "messages": [{"image_url": DATA_URL}], "base64_images": ["PRIVATEBASE64"],
                      "input": {"type": "image", "source": {"type": "base64", "data": "SHORTBASE64"}}, "buffer": b"private-binary"}
        snapshot = sanitize_request_parameters(parameters)
        self.assertEqual(snapshot["image"]["filename"], "reference.png")
        self.assertEqual(snapshot["image"]["size_bytes"], 13)
        self.assertEqual(snapshot["mask"]["filename"], "mask.png")
        text = json.dumps(snapshot)
        for content in ("private-image", "private-mask", "PRIVATEBASE64", "SHORTBASE64", "private-binary", DATA_URL):
            self.assertNotIn(content, text)
        self.assertEqual(upload.file.tell(), 0)

    def test_limits_depth_width_and_total_size(self):
        parameters = {str(index): {"nested": ["字" * 40_000] * 150} for index in range(150)}
        snapshot = sanitize_request_parameters(parameters)
        self.assertIn(TRUNCATED, json.dumps(snapshot, ensure_ascii=False))
        self.assertLess(len(json.dumps(snapshot, ensure_ascii=False)), MAX_TOTAL_CHARS + 3_000)
        deep = {}
        deep["cycle"] = deep
        self.assertIn(TRUNCATED, json.dumps(sanitize_request_parameters(deep)))
        self.assertEqual(sanitize_request_parameters({}), {})


class RequestLoggingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.logs = LogService(self.root / "logs.jsonl")
        # These tests stub the whole protocol handler; queue admission is covered
        # with real protocol parsers in test_generation_api instead.
        admission = mock.patch("services.generation_protocol.prepare_image_call")
        admission.start()
        self.addCleanup(admission.stop)
        for module in ("services.log_service", "test.legacy_image_task_fixture", "services.editable_file_task_service"):
            patch = mock.patch(f"{module}.log_service", self.logs)
            patch.start()
            self.addCleanup(patch.stop)
        for module in (ai, image_tasks):
            for name, value in (("require_identity", lambda _: IDENTITY), ("check_request", lambda _: None)):
                patch = mock.patch.object(module, name, value)
                patch.start()
                self.addCleanup(patch.stop)
        app = FastAPI()
        app.include_router(ai.create_router())
        app.include_router(image_tasks.create_router())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def latest_detail(self):
        return self.logs.list()[0]["detail"]

    def test_generation_logs_only_submitted_fields_without_internal_base_url(self):
        body = {"prompt": "cat\nline two", "model": "gpt-image-2.5-flare", "n": 2, "size": "1024x1024",
                "quality": "high", "response_format": "url", "stream": False, "custom_option": "provided"}
        with mock.patch.object(ai.openai_v1_image_generations, "handle", return_value={"data": []}):
            response = self.client.post("/v1/images/generations", json=body, headers={"Authorization": "Bearer private-header"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.latest_detail()["request_params"], body)
        self.assertNotIn("private-header", self.logs.path.read_text(encoding="utf-8"))

    def test_json_content_types_accepted_by_fastapi_keep_parameters(self):
        body = {"prompt": "cat", "n": 1}
        with mock.patch.object(ai.openai_v1_image_generations, "handle", return_value={}):
            for content_type in ("application/json; charset=utf-8", "application/vnd.test+json"):
                headers = {"Content-Type": content_type}
                response = self.client.post("/v1/images/generations", content=json.dumps(body), headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.latest_detail()["request_params"], body)

    def test_failed_and_filtered_requests_keep_parameters(self):
        body = {"prompt": "cat", "quality": "high"}
        with mock.patch.object(ai.openai_v1_image_generations, "handle", side_effect=RuntimeError("upstream failed")):
            response = self.client.post("/v1/images/generations", json=body)
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(self.latest_detail()["request_params"], body)
        self.assertEqual(self.latest_detail()["status"], "failed")
        with mock.patch.object(ai, "check_request", side_effect=HTTPException(400, "blocked")):
            self.assertEqual(self.client.post("/v1/images/generations", json=body).status_code, 400)
        self.assertEqual(self.latest_detail()["request_params"], body)

    def test_streaming_logs_original_snapshot_even_if_handler_mutates_input(self):
        body = {"model": "text-model", "messages": [{"role": "user", "content": "original"}],
                "stream": True, "temperature": 0.4, "api_key": "secret-api-key"}

        def handler(payload):
            payload["messages"][0]["content"] = "mutated"
            return iter([{"choices": [{"delta": {"content": "ok"}}]}])

        with mock.patch.object(ai.openai_v1_chat_complete, "handle", side_effect=handler):
            response = self.client.post("/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 200)
        recorded = self.latest_detail()["request_params"]
        self.assertEqual(recorded["messages"], body["messages"])
        self.assertEqual(recorded["temperature"], 0.4)
        self.assertEqual(recorded["api_key"], REDACTED)

    def test_late_stream_failure_keeps_parameters(self):
        def items():
            yield {"choices": []}
            raise RuntimeError("stream failed")

        call = LoggedCall(IDENTITY, "/v1/chat/completions", "text-model", "test", request_params={"stream": True})
        with self.assertRaises(RuntimeError):
            list(call.stream(items()))
        self.assertEqual(self.latest_detail()["request_params"], {"stream": True})
        self.assertEqual(self.latest_detail()["status"], "failed")

    def test_prompt_summary_cannot_bypass_image_and_signed_url_redaction(self):
        prompt = f"Edit {DATA_URL} and https://example.test/image.png?token=private-url-token"
        call = LoggedCall(IDENTITY, "/v1/images/generations", "gpt-image-2", "test",
                          request_text=prompt, request_params={"prompt": prompt})
        call.log("完成")
        saved = self.logs.path.read_text(encoding="utf-8")
        self.assertNotIn(DATA_URL, saved)
        self.assertNotIn("private-url-token", saved)

    def test_other_protocols_capture_extra_parameters(self):
        cases = [
            ("/v1/responses", ai.openai_v1_response, {"model": "test", "input": "hello", "max_output_tokens": 100}),
            ("/v1/messages", ai.anthropic_v1_messages, {"model": "test", "messages": [], "max_tokens": 100}),
            ("/v1/search", ai.openai_search, {"prompt": "search"}),
        ]
        for endpoint, protocol, body in cases:
            with self.subTest(endpoint=endpoint), mock.patch.object(protocol, "handle", return_value={}):
                self.assertEqual(self.client.post(endpoint, json=body).status_code, 200)
                self.assertEqual(self.latest_detail()["request_params"], body)

    def test_json_and_multipart_edits_preserve_inputs_without_image_contents(self):
        body = {"prompt": "edit", "images": [{"image_url": DATA_URL}], "quality": "high"}
        with mock.patch.object(ai.openai_v1_image_edit, "handle", return_value={}):
            response = self.client.post("/v1/images/edits", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.latest_detail()["request_params"]["quality"], "high")
            self.assertNotIn(DATA_URL, json.dumps(self.latest_detail()))
            response = self.client.post("/v1/images/edits", data={"prompt": "edit", "n": "2", "quality": "high"},
                                        files=[("image", ("one.png", b"private-one", "image/png")),
                                               ("image", ("two.png", b"private-two", "image/png")),
                                               ("mask", ("mask.png", b"private-mask", "image/png"))])
        self.assertEqual(response.status_code, 200, response.text)
        params = self.latest_detail()["request_params"]
        self.assertEqual(params["n"], "2")
        self.assertEqual([image["filename"] for image in params["image"]], ["one.png", "two.png"])
        self.assertEqual(params["mask"]["size_bytes"], len(b"private-mask"))
        self.assertNotIn("private-one", self.logs.path.read_text(encoding="utf-8"))

    def test_task_api_passes_snapshot_for_generation_and_edit(self):
        service = mock.Mock()
        service.submit_generation.return_value = {"id": "job"}
        service.submit_edit.return_value = {"id": "edit"}
        with mock.patch.object(image_tasks, "image_task_service", service):
            body = {"client_task_id": "job", "prompt": "cat", "size": "1024x1024", "quality": "high"}
            self.assertEqual(self.client.post("/api/image-tasks/generations", json=body).status_code, 200)
            self.assertEqual(service.submit_generation.call_args.kwargs["request_params"], body)
            self.assertEqual(self.client.post("/api/image-tasks/edits", json={**body, "image": DATA_URL}).status_code, 200)
            self.assertNotIn(DATA_URL, json.dumps(service.submit_edit.call_args.kwargs["request_params"]))

    def test_image_input_failure_logs_parameters_for_both_edit_routes(self):
        body = {"client_task_id": "edit", "prompt": "edit", "image": DATA_URL}
        for endpoint, module in (("/v1/images/edits", ai), ("/api/image-tasks/edits", image_tasks)):
            with self.subTest(endpoint=endpoint), mock.patch.object(module, "read_image_sources", side_effect=HTTPException(400, "invalid image")):
                self.assertEqual(self.client.post(endpoint, json=body).status_code, 400)
                self.assertEqual(self.latest_detail()["request_params"]["prompt"], "edit")
                self.assertEqual(self.latest_detail()["status"], "failed")

    def test_editable_file_api_passes_original_fields(self):
        service = mock.Mock()
        service.submit_ppt.return_value = service.submit_psd.return_value = {"id": "file-task"}
        body = {"client_task_id": "file-task", "prompt": "layers", "base64_images": ["PRIVATEBASE64"]}
        with mock.patch.object(ai, "editable_file_task_service", service):
            for kind in ("ppt", "psd"):
                self.assertEqual(self.client.post(f"/v1/{kind}/generations", json=body).status_code, 200)
                params = getattr(service, f"submit_{kind}").call_args.kwargs["request_params"]
                self.assertEqual(params["client_task_id"], "file-task")
                self.assertNotIn("PRIVATEBASE64", json.dumps(params))

    def test_background_tasks_log_on_success_and_failure_and_persist_snapshot(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                handler = mock.Mock(side_effect=RuntimeError("failed") if failure else None,
                                    return_value={"data": [{"url": "http://example.test/output.png"}]})
                service = ImageTaskService(self.root / f"tasks-{failure}.json", edit_handler=handler)
                params = {"prompt": "original\ntext", "quality": "high", "api_key": "private-secret"}
                with mock.patch("test.legacy_image_task_fixture.threading.Thread") as thread:
                    service.submit_edit(IDENTITY, client_task_id="edit", prompt="original\ntext", model="gpt-image-2",
                                        size=None, images=[(b"private-image", "image.png", "image/png")], request_params=params)
                params["prompt"] = "mutated"
                thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
                detail = self.latest_detail()
                self.assertEqual(detail["request_params"]["prompt"], "original\ntext")
                self.assertEqual(detail["status"], "failed" if failure else "success")
                reloaded = ImageTaskService(service.path)
                self.assertEqual(reloaded._tasks["test-admin:edit"]["request_params"], detail["request_params"])
                self.assertNotIn("request_params", reloaded.list_tasks(IDENTITY, ["edit"])["items"][0])
                for private in ("private-secret", "private-image"):
                    self.assertNotIn(private, service.path.read_text(encoding="utf-8"))
                    self.assertNotIn(private, self.logs.path.read_text(encoding="utf-8"))

    def test_resume_failure_logs_poll_parameters_and_original_input(self):
        service = ImageTaskService(self.root / "resume.json")
        service._tasks["test-admin:job"] = {"id": "job", "owner_id": "test-admin", "request_params": {"prompt": "cat"}}
        with mock.patch("services.openai_backend_api.OpenAIBackendAPI") as backend:
            backend.return_value._poll_image_results.return_value = ([], [])
            service._run_resume_poll("test-admin:job", "conversation", 30, IDENTITY, "generate", "gpt-image-2")
        self.assertEqual(self.latest_detail()["request_params"], {
            "task_id": "job", "extra_timeout_secs": 30, "original_request": {"prompt": "cat"},
        })

    def test_editable_file_tasks_receive_and_log_sanitized_input(self):
        service = EditableFileTaskService(self.root / "files.json")
        with mock.patch.object(ai, "editable_file_task_service", service), mock.patch("services.editable_file_task_service.threading.Thread") as thread:
            # 直接调用服务，避免将 Thread mock 应用到 TestClient 自身。
            service.submit_psd(IDENTITY, client_task_id="psd", prompt="layers", base64_images=["PRIVATEBASE64"],
                               request_params={"prompt": "layers", "base64_images": ["PRIVATEBASE64"]})
        with mock.patch("services.editable_file_task_service._editable_access_token", side_effect=RuntimeError("no account")):
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        self.assertEqual(self.latest_detail()["request_params"]["prompt"], "layers")
        self.assertNotIn("PRIVATEBASE64", self.logs.path.read_text(encoding="utf-8"))

    def test_legacy_log_and_empty_parameters_remain_distinguishable(self):
        LoggedCall(IDENTITY, "/v1/test", "test", "old").log("完成")
        self.assertNotIn("request_params", self.latest_detail())
        LoggedCall(IDENTITY, "/v1/test", "test", "new", request_params={}).log("完成")
        self.assertEqual(self.latest_detail()["request_params"], {})


if __name__ == "__main__":
    unittest.main()
