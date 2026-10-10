from __future__ import annotations

from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import tempfile
import tarfile
import time
import unittest
from unittest import mock

from services.generation_context import GenerationContext, checkpoint, save_raw_images
from services.generation_runtime import GenerationRuntime, DEFAULT_QUEUE
from services.generation_store import GenerationStore
from services.protocol.conversation import ConversationRequest, ImageOutput
from services.generation_errors import ImageFailure, GenerationRuntimeError
from test.test_account_scheduling import SchedulingTestCase


class SimulatedProcessDeath(BaseException):
    pass


class RecoveryTests(unittest.TestCase):
    def test_backup_restores_runtime_ledger_and_private_checkpoints(self):
        from services import backup_service
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = GenerationRuntime(root)
            try:
                with mock.patch.object(runtime, "start"):
                    job = runtime.submit(ConversationRequest(model="gpt-image-2", prompt="draw", images=[{"b64_json": "synthetic"}]), GenerationContext({"id": "admin", "role": "admin"}, "test", "backup"))
                child = runtime.store.rows("SELECT id FROM tasks WHERE job_id=?", (job,))[0]["id"]
                runtime.store.claim(child)
                runtime.store.checkpoint(child, "submitting", account_id="original")
                with mock.patch.object(backup_service, "DATA_DIR", root), mock.patch("services.generation_runtime._runtime", runtime):
                    payload = backup_service.backup_service._build_backup_archive({"include": {}}, trigger="test")
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                    names = archive.getnames()
                    self.assertIn(f"data/generation-tasks/{job}/inputs.json", names)
                    restored_file = root / "restored.sqlite"
                    restored_file.write_bytes(archive.extractfile("data/generation-runtime.sqlite").read())
                restored = GenerationStore(restored_file)
                try:
                    restored.recover()
                    self.assertEqual(restored.task(child)["status"], "uncertain")
                    self.assertEqual(restored.quota("admin")["image_quota_reserved"], 1)
                    self.assertEqual(restored.quota("admin")["image_quota_used"], 0)
                finally:
                    restored.close()
            finally:
                runtime.stop()
                runtime.store.close()

    def test_restart_at_each_checkpoint_never_resubmits_unknown_or_known_submission(self):
        for phase in ("account_selected", "submitting", "submitted", "raw_saved", "output_saved"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                counts = {"generation": 0, "recovery": 0}
                died = False
                runtime = None
                def execute(request, index, total, row):
                    nonlocal died
                    if not died:
                        died = True
                        checkpoint("account_selected", account_id="original")
                        if phase != "account_selected":
                            checkpoint("submitting")
                            counts["generation"] += 1
                        if phase in {"submitted", "raw_saved", "output_saved"}:
                            checkpoint("submitted", conversation_id="original-handle")
                        if phase in {"raw_saved", "output_saved"}:
                            save_raw_images([{"b64_json": "synthetic"}])
                        if phase == "output_saved":
                            runtime._write_json(row["job_id"], f"{row['id']}-output.json", [asdict(ImageOutput(kind="result", model=request.model, index=1, total=1, data=[{"url": "/images/saved.png"}]))])
                            checkpoint("output_saved")
                        raise SimulatedProcessDeath()
                    if row["recovery"] == "recovering_result":
                        counts["recovery"] += 1
                        self.assertEqual(row["account_id"], "original")
                        self.assertEqual(row["conversation_id"], "original-handle")
                    else:
                        counts["generation"] += 1
                    return [ImageOutput(kind="result", model=request.model, index=1, total=1, data=[{"url": "/images/saved.png"}])]
                runtime = GenerationRuntime(root, executor=execute)
                try:
                    request = ConversationRequest(model="gpt-image-2", prompt="draw")
                    context = GenerationContext({"id": "admin", "role": "admin"}, "test", phase)
                    job = runtime.submit(request, context)
                    deadline = time.time() + 3
                    while (not died or runtime._active) and time.time() < deadline:
                        time.sleep(.01)
                    runtime.stop()
                    runtime.start()
                    deadline = time.time() + 3
                    while runtime.jobs(context.identity, [job])[0]["status"] in {"queued", "running", "uncertain"} and time.time() < deadline:
                        time.sleep(.01)
                    result = runtime.jobs(context.identity, [job])[0]
                    self.assertEqual(result["status"], "error" if phase == "submitting" else "success")
                    self.assertEqual(counts["generation"], 1)
                    self.assertEqual(runtime.store.quota("admin")["image_quota_used"], 0 if phase == "submitting" else 1)
                    if phase == "submitting":
                        self.assertEqual(counts["recovery"], 0)
                        self.assertEqual(runtime.store.quota("admin")["image_quota_reserved"], 0)
                        self.assertEqual(result["error_code"], "image_result_unrecoverable")
                    self.assertEqual(runtime.submit(request, GenerationContext(context.identity, "test", phase)), job)
                finally:
                    runtime.stop()
                    runtime.store.close()

    def test_legacy_migration_is_read_only_and_does_not_charge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "image_tasks.json"
            legacy.write_text(json.dumps({"tasks": [{"id": "old-success", "owner_id": "admin", "status": "success", "data": [{"url": "/images/old.png"}]}, {"id": "old-running", "owner_id": "admin", "status": "running"}]}), encoding="utf-8")
            before = hashlib.sha256(legacy.read_bytes()).hexdigest()
            runtime = GenerationRuntime(root)
            try:
                runtime.start()
                tasks = {item["id"]: item for item in runtime.jobs({"id": "admin"})}
                self.assertEqual(tasks["old-success"]["status"], "success")
                self.assertIn("已中断", tasks["old-running"]["error"])
                self.assertEqual(runtime.store.quota("admin")["image_quota_used"], 0)
                runtime.stop()
                runtime.start()
                self.assertEqual(len(runtime.jobs({"id": "admin"})), 2)
                self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), before)
            finally:
                runtime.stop()
                runtime.store.close()


class AccountCooldownTests(SchedulingTestCase):
    def test_retry_budget_refresh_once_and_ambiguous_submission_cooldown(self):
        from services.generation_execution import execute_image
        from services.protocol import conversation
        from utils.helper import UpstreamHTTPError
        for status in (400, 401, 429, 503):
            with self.subTest(status=status):
                for index in range(4):
                    token = f"{status}-{index}"
                    self.add(token)
                calls = []
                def generate(backend, *_args):
                    calls.append(backend.access_token)
                    checkpoint("submitting")
                    raise UpstreamHTTPError("generation", status, {})
                def backend(access_token):
                    result = mock.Mock()
                    result.access_token = access_token
                    return result
                with mock.patch.object(conversation, "account_service", self.service), mock.patch.object(conversation, "OpenAIBackendAPI", side_effect=backend), mock.patch.object(conversation, "stream_image_outputs", side_effect=generate), mock.patch.object(self.service, "refresh_access_token", side_effect=lambda token, **_kw: token) as refresh:
                    with self.assertRaises(conversation.ImageGenerationError) as raised:
                        execute_image(ConversationRequest(model="gpt-image-2", prompt="draw"), 1, 1, {"deadline": time.time()})
                self.assertEqual(len(calls), 3 if status in {401, 429} else 1)
                self.assertEqual(len(set(calls)), len(calls))
                self.assertEqual(refresh.call_count, len(calls) if status == 401 else 0)
                if status == 503:
                    self.assertTrue(raised.exception.submission_uncertain)
                    account = self.service.get_account(calls[0])
                    self.assertEqual(account["image_blocks"]["web"]["reason"], "transient")
                if status == 400:
                    self.assertFalse(self.service.get_account(calls[0]).get("image_blocks"))
                for token in self.service.list_tokens():
                    self.service.update_account(token, {"status": "禁用"}, quiet=True)

    def pick(self, model="gpt-image-2", channel="web"):
        token = self.service.acquire_governed_image_token(model=model, channel=channel, excluded_ids=set(), deadline=time.time())
        self.service.release_image_slot(token)
        return token

    def test_model_scope_cooldown_expiry_and_disabled_account(self):
        self.add("one", source_type="codex", type="plus", priority=10)
        self.add("two", source_type="codex", type="plus")
        self.service.record_image_failure("one", "gpt-image-2", "web", ImageFailure("model_permission", True, scope="model"))
        self.assertEqual(self.pick(), "two")
        self.assertEqual(self.pick("gpt-image-2.5-flare"), "one")
        self.assertEqual(self.pick("codex-gpt-image-2", "codex"), "one")
        self.service.clear_image_blocks("one")
        self.service.record_image_failure("one", "gpt-image-2", "web", ImageFailure("rate_limited", True, 180))
        self.assertEqual(self.pick(), "two")
        self.service.update_account("one", {"image_blocks": {"web": {"reason": "rate_limited", "until": time.time() - 1}}}, quiet=True)
        self.assertEqual(self.pick(), "one")
        self.service.update_account("one", {"status": "禁用"}, quiet=True)
        self.assertEqual(self.pick(), "two")

    def test_unknown_403_does_not_disable_or_remove_account(self):
        self.add("one")
        from services.generation_errors import classify_image_error
        from utils.helper import UpstreamHTTPError
        self.service.record_image_failure("one", "gpt-image-2", "web", classify_image_error(UpstreamHTTPError("test", 403, {})))
        account = self.service.get_account("one")
        self.assertEqual(account["status"], "正常")
        self.assertEqual(account["image_blocks"]["web"]["reason"], "access_denied")
        with self.assertRaises(GenerationRuntimeError):
            self.pick()
        self.service.clear_image_blocks("one")
        self.assertEqual(self.pick(), "one")
