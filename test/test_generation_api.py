from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from api import ai, accounts, image_tasks, generation_runtime, support
from api.errors import install_exception_handlers
from services.auth_service import AuthService
from services.config import config
from services.generation_context import checkpoint
from services.generation_errors import GenerationRuntimeError
from services.generation_runtime import GenerationRuntime, DEFAULT_QUEUE
from services.log_service import LogService
from services.request_log import sanitize_request_parameters
from services.protocol.conversation import ImageOutput
from services.storage.json_storage import JSONStorageBackend


class GenerationAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = AuthService(JSONStorageBackend(self.root / "accounts.json"))
        self.calls = []
        self.hold = threading.Event()
        self.hold.set()
        self.runtime = GenerationRuntime(self.root, identity_resolver=lambda owner: {"id": "admin", "role": "admin"} if owner == "admin" else self.auth.find_identity(owner), executor=self.execute)
        self.logs = LogService(self.root / "calls.jsonl")
        self.patches = [mock.patch("services.generation_runtime._runtime", self.runtime),
                        mock.patch("services.log_service.log_service", self.logs),
                        mock.patch.object(support, "auth_service", self.auth), mock.patch.object(accounts, "auth_service", self.auth),
                        mock.patch.object(ai, "check_request"), mock.patch.object(image_tasks, "check_request")]
        for patch in self.patches:
            patch.start()
        app = FastAPI()
        install_exception_handlers(app)
        for module in (ai, accounts, image_tasks, generation_runtime):
            app.include_router(module.create_router())
        self.client = TestClient(app)
        self.admin = {"Authorization": f"Bearer {config.auth_key}"}
        output = BytesIO()
        Image.new("RGB", (16, 16), "red").save(output, format="PNG")
        self.png = base64.b64encode(output.getvalue()).decode()

    def tearDown(self):
        self.hold.set()
        self.client.close()
        self.runtime.stop()
        self.runtime.store.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def execute(self, request, index, total, row):
        self.hold.wait(5)
        self.calls.append(row["id"])
        return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "/images/synthetic.png", "b64_json": self.png, "revised_prompt": request.prompt}])]

    def key(self, limit=None):
        response = self.client.post("/api/auth/users", headers=self.admin, json={"name": "test", "image_quota_limit": limit, "image_concurrency_limit": 2})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["item"], {"Authorization": "Bearer " + response.json()["key"]}

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("timeout")
            time.sleep(.02)

    def test_user_key_concurrency_above_64_round_trips_and_still_requires_positive_integers(self):
        created = self.client.post("/api/auth/users", headers=self.admin, json={"name": "large-concurrency", "image_concurrency_limit": 128})
        self.assertEqual(created.status_code, 200, created.text)
        item = created.json()["item"]
        self.assertEqual(item["image_concurrency_limit"], 128)
        path = f"/api/auth/users/{item['id']}"
        updated = self.client.post(path, headers=self.admin, json={"image_concurrency_limit": 256})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["item"]["image_concurrency_limit"], 256)
        for value in (0, -1, True, 65.5, "128"):
            with self.subTest(invalid=value):
                self.assertEqual(self.client.post("/api/auth/users", headers=self.admin, json={"image_concurrency_limit": value}).status_code, 422)
                self.assertEqual(self.client.post(path, headers=self.admin, json={"image_concurrency_limit": value}).status_code, 422)
                self.assertEqual(self.runtime.store.quota(item["id"])["image_concurrency_limit"], 256)
        reset = self.client.post(path, headers=self.admin, json={"image_concurrency_limit": None})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertIsNone(reset.json()["item"]["image_concurrency_limit"])

    def test_all_sync_and_streaming_protocols_return_task_id_and_deduplicate(self):
        paths = [("/v1/images/generations", {"prompt": "draw"}),
                 ("/v1/images/edits", {"prompt": "draw", "images": [{"b64_json": self.png}]}),
                 ("/v1/chat/completions", {"messages": [{"role": "user", "content": "draw"}]}),
                 ("/v1/responses", {"input": "draw"})]
        for streaming in (False, True):
            for path, body in paths:
                with self.subTest(path=path, streaming=streaming):
                    headers = {**self.admin, "Idempotency-Key": f"repeated-{streaming}"}
                    payload = {**body, "model": "gpt-image-2", "stream": streaming}
                    before = len(self.logs.list(type="call"))
                    first = self.client.post(path, headers=headers, json=payload)
                    self.assertEqual(first.status_code, 200, first.text)
                    self.assertTrue(first.headers.get("X-Image-Task-Id"))
                    records = self.logs.list(type="call")
                    self.assertEqual(len(records), before + 1)
                    self.assertEqual(records[0]["detail"]["request_params"], sanitize_request_parameters(payload))
                    self.assertEqual(records[0]["detail"]["image_task_id"], first.headers["X-Image-Task-Id"])
                    self.assertEqual(records[0]["detail"]["outcome"], "success")
                    self.assertEqual(records[0]["detail"]["phase"], "output_saved")
                    duplicate = self.client.post(path, headers=headers, json=payload)
                    self.assertEqual(first.headers["X-Image-Task-Id"], duplicate.headers["X-Image-Task-Id"])
                    self.assertEqual(len(self.logs.path.read_text(encoding="utf-8").splitlines()), before + 2)
                    if streaming:
                        self.assertIn("data:", first.text)
                    else:
                        self.assertIsInstance(first.json(), dict)
        # Protocol-level 'stream' does not change normalized generation content.
        self.assertEqual(len(self.calls), 8)

    def test_retry_diagnostics_stay_inside_one_failed_call(self):
        from services.generation_errors import RetryImage
        attempts = []

        def rejected(request, index, total, row):
            attempts.append(row["id"])
            if len(attempts) == 1:
                checkpoint("rejected", attempt=1, error_category="rate_limited")
                raise RetryImage()
            checkpoint("account_selected", attempt=2)
            raise GenerationRuntimeError("合成参数错误", "invalid_image_request", 400)

        self.runtime.execute = rejected
        response = self.client.post("/v1/images/generations", headers=self.admin, json={"prompt": "draw"})
        self.assertEqual(response.status_code, 400, response.text)
        records = self.logs.list(type="call")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(self.logs.path.read_text(encoding="utf-8").splitlines()), 1)
        detail = records[0]["detail"]
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["error_category"], "invalid_request")
        self.assertEqual(detail["phase"], "account_selected")
        self.assertEqual(detail["retry_count"], 1)
        self.assertEqual(detail["outcome"], "error")
        self.assertEqual(len(attempts), 2)

    def test_multi_image_partial_success_has_one_call_with_all_children(self):
        original_execute = self.runtime.execute

        def partial(request, index, total, row):
            if row["ordinal"] == 1:
                raise GenerationRuntimeError("合成参数错误", "invalid_image_request", 400)
            return original_execute(request, index, total, row)

        self.runtime.execute = partial
        response = self.client.post("/v1/images/generations", headers=self.admin, json={"prompt": "draw", "n": 2})
        self.assertEqual(response.status_code, 200, response.text)
        records = self.logs.list(type="call")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(self.logs.path.read_text(encoding="utf-8").splitlines()), 1)
        self.assertCountEqual([task["outcome"] for task in records[0]["detail"]["image_tasks"]], ["success", "error"])
        self.assertTrue(records[0]["detail"]["urls"])

    def test_web_background_generation_still_records_completion(self):
        response = self.client.post("/api/image-tasks/generations", headers=self.admin,
                                    json={"client_task_id": "web-logging", "prompt": "draw"})
        self.assertEqual(response.status_code, 200, response.text)
        self.wait_for(lambda: len(self.logs.list(type="call")) == 1)
        detail = self.logs.list(type="call")[0]["detail"]
        self.assertEqual(detail["endpoint"], "/api/image-tasks/generations")
        self.assertEqual(detail["status"], "success")
        self.assertEqual(detail["request_params"]["prompt"], "draw")

    def test_unqueryable_submission_automatically_finishes_with_one_call_log(self):
        def interrupted(request, index, total, row):
            self.calls.append(row["id"])
            checkpoint("submitting")
            raise TimeoutError("synthetic submission timeout")

        self.runtime.execute = interrupted
        response = self.client.post("/v1/images/generations", headers=self.admin, json={"prompt": "draw"})
        self.assertEqual(response.status_code, 502, response.text)
        records = self.logs.list(type="call")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(self.logs.path.read_text(encoding="utf-8").splitlines()), 1)
        detail = records[0]["detail"]
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["phase"], "submitting")
        self.assertEqual(detail["outcome"], "error")
        self.assertEqual(detail["recovery_status"], "auto_failed")
        self.assertEqual(detail["error_code"], "image_result_unrecoverable")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.runtime.store.quota("admin")["image_quota_reserved"], 0)

    def test_all_image_protocols_wait_for_automatic_recovery_without_resubmitting(self):
        original_execute = self.runtime.execute
        submitted, recovered = [], []

        def interrupted(request, index, total, row):
            if row["recovery"] != "recovering_result":
                submitted.append(row["id"])
                checkpoint("submitted", account_id="original", conversation_id="original-handle")
                raise TimeoutError("synthetic result timeout")
            recovered.append(row["id"])
            self.assertEqual(row["account_id"], "original")
            self.assertEqual(row["conversation_id"], "original-handle")
            return original_execute(request, index, total, row)

        self.runtime.execute = interrupted
        paths = [("/v1/images/generations", {"prompt": "draw"}),
                 ("/v1/images/edits", {"prompt": "draw", "images": [{"b64_json": self.png}]}),
                 ("/v1/chat/completions", {"messages": [{"role": "user", "content": "draw"}]}),
                 ("/v1/responses", {"input": "draw"})]
        with mock.patch("services.generation_recovery.RECOVERY_DELAYS", (0, 0, 0)):
            for stream in (False, True):
                for path, body in paths:
                    response = self.client.post(path, headers=self.admin, json={**body, "model": "gpt-image-2", "stream": stream})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertNotIn("response.failed", response.text)
                    detail = self.logs.list(type="call")[0]["detail"]
                    self.assertEqual(detail["status"], "success")
                    self.assertEqual(detail["recovery_attempts"], 1)
        self.assertEqual(submitted, recovered)
        self.assertEqual(len(submitted), 8)
        self.assertEqual(len(self.logs.path.read_text(encoding="utf-8").splitlines()), 8)
        self.assertEqual(self.runtime.store.quota("admin")["image_quota_used"], 8)
        self.assertEqual(self.runtime.store.quota("admin")["image_quota_reserved"], 0)

    def test_web_batch_rejects_whole_request_then_exposes_individual_tasks(self):
        item, headers = self.key(1)
        body = {"client_task_id": "batch-1", "client_task_ids": ["child-1", "child-2"], "prompt": "draw"}
        response = self.client.post("/api/image-tasks/generations", headers=headers, json=body)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()["error"]["code"], "image_quota_exceeded")
        self.assertEqual(self.runtime.store.quota(item["id"])["image_quota_reserved"], 0)
        self.assertEqual(self.calls, [])
        self.client.post(f"/api/auth/users/{item['id']}", headers=self.admin, json={"image_quota_limit": 2})
        response = self.client.post("/api/image-tasks/generations", headers=headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([task["id"] for task in response.json()["items"]], body["client_task_ids"])
        self.wait_for(lambda: self.runtime.store.quota(item["id"])["image_quota_used"] == 2)
        queried = self.client.get("/api/image-tasks?ids=child-1,child-2,missing", headers=headers).json()
        self.assertEqual(queried["missing_ids"], ["missing"])
        self.assertTrue(all(task["status"] == "success" for task in queried["items"]))
        duplicate = self.client.post("/api/image-tasks/generations", headers=headers, json=body)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(len(self.calls), 2)
        conflict = self.client.post("/api/image-tasks/generations", headers=headers, json={**body, "prompt": "different"})
        self.assertEqual(conflict.status_code, 409)

    def test_pre_admission_refusals_have_separate_statistics_without_charging(self):
        item, headers = self.key(0)
        invalid = self.client.post("/v1/images/generations", headers=headers, json={"prompt": ""})
        self.assertEqual(invalid.status_code, 422)
        with mock.patch.object(ai, "check_request", side_effect=HTTPException(400, {"error": "blocked", "code": "content_filter"})):
            denied = self.client.post("/v1/images/generations", headers=headers, json={"prompt": "draw"})
        self.assertEqual(denied.status_code, 400)
        rejected = self.client.post("/v1/images/generations", headers=headers, json={"prompt": "draw"})
        self.assertEqual(rejected.status_code, 429)
        summary = self.client.get("/api/runtime/statistics", headers=self.admin).json()["summary"]
        self.assertEqual((summary["requests"], summary["invalid_request"], summary["content_rejected"], summary["local_rejection"]), (3, 1, 1, 1))
        self.assertIsNone(summary["platform_success_rate"])
        self.assertEqual(self.runtime.store.quota(item["id"])["image_quota_used"], 0)
        self.assertEqual(self.calls, [])

    def test_key_rotation_history_deletion_and_permissions(self):
        item, headers = self.key(2)
        response = self.client.post("/v1/images/generations", headers=headers, json={"prompt": "draw"})
        self.assertEqual(response.status_code, 200)
        task_id = response.headers["X-Image-Task-Id"]
        self.assertEqual(self.client.get("/api/runtime/statistics", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/runtime/tasks", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/auth/users", headers=headers, json={}).status_code, 403)
        updated = self.client.post(f"/api/auth/users/{item['id']}", headers=self.admin, json={"key": "synthetic-new-key"})
        self.assertEqual(updated.json()["item"]["image_quota_used"], 1)
        self.assertEqual(self.client.get("/api/image-quota", headers=headers).status_code, 401)
        rotated = {"Authorization": "Bearer synthetic-new-key"}
        self.assertEqual(self.client.get("/api/image-quota", headers=rotated).json()["image_quota_remaining"], 1)
        self.assertEqual(self.client.post(f"/api/auth/users/{item['id']}", headers=self.admin, json={"image_quota_limit": 0}).status_code, 400)
        other, raw = self.auth.create_key(role="user", name="other")
        other_headers = {"Authorization": f"Bearer {raw}"}
        self.assertEqual(self.client.get(f"/api/image-tasks?ids={task_id}", headers=other_headers).json()["items"], [])
        self.assertEqual(self.client.post(f"/api/image-tasks/{task_id}/cancel", headers=other_headers).status_code, 404)
        self.client.delete(f"/api/auth/users/{item['id']}", headers=self.admin)
        self.assertEqual(self.runtime.store.quota(item["id"])["image_quota_used"], 1)
        self.assertEqual(len(self.runtime.store.rows("SELECT * FROM settlements WHERE owner=?", (item["id"],))), 1)

    def test_disconnect_from_iterator_does_not_cancel_worker_or_double_charge(self):
        from services.generation_context import GenerationContext
        from services.protocol.conversation import ConversationRequest
        self.hold.clear()
        request = ConversationRequest(prompt="draw", model="gpt-image-2")
        context = GenerationContext({"id": "admin", "role": "admin"}, "/v1/images/generations", "disconnect")
        job = self.runtime.submit(request, context)
        outputs = self.runtime.outputs(job, request)
        next(outputs)
        outputs.close()
        self.hold.set()
        self.wait_for(lambda: self.runtime.store.quota("admin")["image_quota_used"] == 1)
        self.assertEqual(self.runtime.submit(request, GenerationContext(context.identity, context.endpoint, "disconnect")), job)
        self.assertEqual(len(self.calls), 1)

    def test_key_disabled_after_account_selection_is_rechecked_before_submission(self):
        item, headers = self.key(1)
        def blocked(request, index, total, row):
            checkpoint("account_selected", account_id="synthetic-account")
            self.auth.update_key(item["id"], {"enabled": False})
            checkpoint("submitting")
            self.fail("must not submit disabled key")
        self.runtime.execute = blocked
        response = self.client.post("/v1/images/generations", headers=headers, json={"prompt": "draw"})
        self.assertEqual(response.status_code, 403, response.text)
        quota = self.runtime.store.quota(item["id"])
        self.assertEqual((quota["image_quota_used"], quota["image_quota_reserved"]), (0, 0))

    def test_started_responses_stream_has_protocol_failure_event(self):
        def rejected(request, index, total, row):
            time.sleep(.05)
            raise GenerationRuntimeError("合成参数错误", "invalid_image_request", 400)
        self.runtime.execute = rejected
        response = self.client.post("/v1/responses", headers=self.admin, json={"model": "gpt-image-2", "input": "draw", "stream": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers.get("X-Image-Task-Id"))
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]
        failed = next(event for event in events if event.get("type") == "response.failed")
        self.assertEqual(failed["response"]["error"]["code"], "invalid_image_request")
        self.assertFalse(any(event.get("type") == "response.completed" for event in events))
        records = self.logs.list(type="call")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(self.logs.path.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(records[0]["detail"]["status"], "failed")
        self.assertEqual(records[0]["detail"]["error_category"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
