from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from services.generation_context import GenerationContext
from services.generation_runtime import GenerationRuntime
from services.generation_statistics import runtime_statistics
from services.protocol.conversation import ConversationRequest, ImageOutput


class AutomaticRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.identity = {"id": "synthetic-key", "role": "user", "enabled": True}
        self.now = time.time()
        self.clock = mock.patch("services.generation_recovery.time", SimpleNamespace(time=lambda: self.now))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.reads = []
        self.runtime = GenerationRuntime(self.root, identity_resolver=lambda _: self.identity, executor=self.execute)
        self.runtime._pool = mock.Mock()
        self.runtime._pool.submit.side_effect = lambda fn, *args: fn(*args)

    def tearDown(self):
        self.runtime.stop()
        self.runtime.store.close()
        self.temp.cleanup()

    def execute(self, request, index, total, row):
        self.reads.append(row)
        self.assertEqual(row["recovery"], "recovering_result")
        return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "/images/recovered.png"}])]

    def pending(self, *, handle="original-handle", account="original", model="gpt-image-2"):
        request = ConversationRequest(model=model, prompt="synthetic recovery")
        with mock.patch.object(self.runtime, "start"):
            job = self.runtime.submit(request, GenerationContext(self.identity, "/v1/images/generations"))
        child = self.runtime.store.rows("SELECT id FROM tasks WHERE job_id=?", (job,))[0]["id"]
        self.runtime.store.claim(child)
        self.runtime.store.checkpoint(child, "submitted" if handle else "submitting", account_id=account, conversation_id=handle)
        self.runtime.store.finish(child, status="uncertain", error="synthetic timeout")
        return job, child

    def next_attempt(self, child):
        self.runtime.dispatch_once()
        row = self.runtime.store.task(child)
        self.assertIsNotNone(row["recovery_next_at"])
        self.now = row["recovery_next_at"]
        self.runtime.dispatch_once()

    def test_backoff_exhaustion_releases_once_and_rejects_late_result(self):
        job, child = self.pending()
        def fail(*args):
            self.reads.append(args[-1])
            raise TimeoutError("synthetic read timeout")
        self.runtime.execute = fail
        for attempt, delay in enumerate((30, 60, 120), 1):
            self.runtime.dispatch_once()
            row = self.runtime.store.task(child)
            self.assertEqual(row["recovery_next_at"], self.now + delay)
            self.assertEqual(self.runtime._active, {})
            self.assertEqual(self.runtime.store.quota(self.identity["id"])["image_quota_reserved"], 1)
            self.runtime.dispatch_once()
            self.assertEqual(len(self.reads), attempt - 1)
            self.now += delay
            self.runtime.dispatch_once()
            self.assertEqual(self.runtime.store.task(child)["recovery_attempts"], attempt)
        self.runtime.dispatch_once()
        row = self.runtime.store.task(child)
        self.assertEqual((row["status"], row["error_code"]), ("error", "image_result_recovery_exhausted"))
        self.assertEqual(len(self.reads), 3)
        self.assertTrue(all(row["account_id"] == "original" and row["conversation_id"] == "original-handle" for row in self.reads))
        self.runtime.store.finish(child, status="success", output=[{"kind": "result", "data": [{"url": "/late"}]}])
        self.runtime.dispatch_once()
        self.assertEqual(self.runtime.store.quota(self.identity["id"])["image_quota_used"], 0)
        self.assertEqual(self.runtime.store.quota(self.identity["id"])["image_quota_reserved"], 0)
        self.assertEqual(len(self.runtime.store.rows("SELECT * FROM settlements WHERE task_id=?", (child,))), 1)
        self.assertEqual(self.runtime.jobs(self.identity, [job])[0]["data"], [])

    def test_success_is_delivered_and_settled_once(self):
        job, child = self.pending()
        self.next_attempt(child)
        self.assertEqual(self.runtime.store.task(child)["status"], "success")
        result = self.runtime.jobs(self.identity, [job])[0]
        self.assertEqual(result["data"][0]["url"], "/images/recovered.png")
        self.assertEqual(result["recovery_status"], "completed")
        self.assertFalse(result["recovery_active"])
        self.assertEqual(result["recovery_attempts"], 1)
        self.runtime.dispatch_once()
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(self.runtime.store.quota(self.identity["id"])["image_quota_used"], 1)

    def test_result_arriving_after_recovery_deadline_is_not_delivered_or_charged(self):
        job, child = self.pending()
        def late_result(request, index, total, row):
            self.now = row["recovery_deadline"] + 1
            return self.execute(request, index, total, row)
        self.runtime.execute = late_result
        with mock.patch("services.generation_runtime.time", SimpleNamespace(time=lambda: self.now)):
            self.next_attempt(child)
        self.assertEqual(self.runtime.store.task(child)["error_code"], "image_result_recovery_expired")
        self.assertEqual(self.runtime.jobs(self.identity, [job])[0]["data"], [])
        quota = self.runtime.store.quota(self.identity["id"])
        self.assertEqual((quota["image_quota_used"], quota["image_quota_reserved"]), (0, 0))

    def test_missing_original_account_automatically_ends_without_switching_accounts(self):
        from services.generation_errors import GenerationRuntimeError
        _, child = self.pending()
        self.runtime._requires_account = True
        pool = mock.Mock()
        pool.try_acquire_image_token.side_effect = GenerationRuntimeError("original unavailable", "image_account_unavailable", 502)
        self.runtime.account_pool = pool
        self.next_attempt(child)
        self.assertEqual(pool.try_acquire_image_token.call_args.kwargs["preferred_id"], "original")
        self.assertEqual(pool.try_acquire_image_token.call_count, 1)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.runtime.store.task(child)["error_code"], "image_result_unrecoverable")
        self.assertEqual(self.runtime.store.quota(self.identity["id"])["image_quota_reserved"], 0)

    def test_restart_during_recovery_preserves_consumed_attempt_and_deadline(self):
        _, child = self.pending()
        self.runtime._pool.submit.side_effect = None
        self.next_attempt(child)
        before = self.runtime.store.task(child)
        self.assertEqual((before["status"], before["recovery_attempts"]), ("running", 1))
        self.runtime._active.clear()
        self.runtime.store.recover()
        self.runtime._pool.submit.side_effect = lambda fn, *args: fn(*args)
        self.runtime.dispatch_once()
        after = self.runtime.store.task(child)
        self.assertEqual((after["status"], after["recovery_attempts"]), ("success", 2))
        self.assertEqual(after["recovery_deadline"], before["recovery_deadline"])

    def test_restart_preserves_backoff_deadline_and_attempt_budget(self):
        _, child = self.pending()
        self.runtime.dispatch_once()
        before = self.runtime.store.task(child)
        self.runtime.store.close()
        from services.generation_store import GenerationStore
        self.runtime.store = GenerationStore(self.root / "generation-runtime.sqlite")
        self.runtime.recovery.store = self.runtime.store
        self.runtime.store.recover()
        self.now += 5
        self.runtime.dispatch_once()
        after = self.runtime.store.task(child)
        for field in ("recovery_started", "recovery_deadline", "recovery_next_at", "recovery_attempts"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(self.reads, [])
        self.now = before["recovery_next_at"]
        self.runtime.dispatch_once()
        self.assertEqual(len(self.reads), 1)

    def test_deadline_includes_waiting_for_capacity_and_old_manual_records_migrate(self):
        _, child = self.pending()
        with self.runtime.store.lock:
            self.runtime.store.db.execute("UPDATE tasks SET recovery='awaiting_confirmation' WHERE id=?", (child,))
        self.runtime.dispatch_once()
        row = self.runtime.store.task(child)
        self.assertEqual(row["recovery"], "auto_pending")
        self.assertEqual(row["recovery_deadline"], self.now + 600)
        self.runtime._active["other"] = self.identity["id"]
        self.runtime.settings_getter = lambda: {"global_concurrency": 1, "key_concurrency": 1}
        self.now += 30
        self.runtime.dispatch_once()
        self.assertEqual(self.runtime.store.task(child)["status"], "queued")
        self.assertEqual(self.reads, [])
        self.now += 570
        self.runtime.dispatch_once()
        self.assertEqual(self.runtime.store.task(child)["error_code"], "image_result_recovery_expired")
        self.assertEqual(self.runtime.store.quota(self.identity["id"])["image_quota_reserved"], 0)
        self.runtime._active.clear()

    def test_unqueryable_submission_never_reaches_executor(self):
        for values in ({"handle": ""}, {"account": ""}, {"model": "codex-gpt-image-2"}):
            with self.subTest(values=values):
                _, child = self.pending(**values)
                self.runtime.dispatch_once()
                row = self.runtime.store.task(child)
                self.assertEqual((row["status"], row["recovery"]), ("error", "auto_failed"))
                self.assertEqual(row["error_code"], "image_result_unrecoverable")
        self.assertEqual(self.reads, [])
        self.assertEqual(self.runtime.store.quota(self.identity["id"])["image_quota_reserved"], 0)

    def test_saved_outputs_recover_without_original_account_or_generation(self):
        job, child = self.pending(handle="", account="", model="codex-gpt-image-2")
        output = asdict(ImageOutput(kind="result", model="codex-gpt-image-2", index=1, total=1, data=[{"url": "/images/saved.png"}]))
        # Crash after the atomic file write but before persisting its DB phase.
        self.runtime._write_json(job, f"{child}-output.json", [output])
        self.runtime.dispatch_once()
        self.assertEqual(self.reads, [])
        self.assertEqual(self.runtime.store.task(child)["status"], "success")
        self.assertEqual(self.runtime.store.quota(self.identity["id"])["image_quota_used"], 1)

    def test_original_result_can_be_read_after_three_generation_accounts(self):
        from services.generation_execution import execute_image
        from services.protocol import conversation
        pool = mock.Mock()
        pool.get_account.return_value = {"pool_account_id": "original", "email": "synthetic@example.test"}
        backend = mock.Mock()
        backend.resolve_conversation_image_urls.return_value = ["https://example.test/result.png"]
        backend.download_image_bytes.return_value = [b"synthetic"]
        row = {"_account_pool": pool, "_account_token": "synthetic", "account_id": "original", "conversation_id": "handle",
               "attempted_accounts": json.dumps(["one", "two", "original"]), "recovery": "recovering_result"}
        with mock.patch.object(conversation, "OpenAIBackendAPI", return_value=backend), \
                mock.patch.object(conversation, "format_image_result", return_value={"data": [{"url": "/images/result.png"}]}), \
                mock.patch.object(conversation, "stream_image_outputs") as generate:
            result = execute_image(ConversationRequest(model="gpt-image-2", prompt="draw"), 1, 1, row)
        generate.assert_not_called()
        self.assertEqual(len(result), 1)
        self.assertEqual(backend.resolve_conversation_image_urls.call_args.args[0], "handle")

    def test_recovery_saves_raw_images_before_local_failure_and_reuses_them(self):
        from services.generation_context import ExecutionCheckpoint, execution_checkpoint
        from services.generation_execution import execute_image
        from services.protocol import conversation
        saved = []
        state = ExecutionCheckpoint(persist=lambda **values: None, raw_writer=lambda images: saved.extend(images))
        token = execution_checkpoint.set(state)
        self.addCleanup(execution_checkpoint.reset, token)
        pool = mock.Mock()
        pool.get_account.return_value = {"pool_account_id": "original"}
        backend = mock.Mock()
        backend.resolve_conversation_image_urls.return_value = ["https://example.test/result.png"]
        backend.download_image_bytes.return_value = [b"synthetic"]
        row = {"_account_pool": pool, "_account_token": "synthetic", "account_id": "original", "conversation_id": "handle", "recovery": "recovering_result"}
        request = ConversationRequest(model="gpt-image-2", prompt="draw")
        with mock.patch.object(conversation, "OpenAIBackendAPI", return_value=backend), \
                mock.patch.object(conversation, "format_image_result", side_effect=[OSError("synthetic storage failure"), {"data": [{"url": "/images/recovered.png"}]}]), \
                mock.patch.object(conversation, "stream_image_outputs") as generate:
            with self.assertRaises(Exception):
                execute_image(request, 1, 1, row)
            self.assertTrue(saved)
            result = execute_image(request, 1, 1, {**row, "raw_images": saved})
        generate.assert_not_called()
        backend.resolve_conversation_image_urls.assert_called_once()
        backend.download_image_bytes.assert_called_once()
        self.assertEqual(result[0].data[0]["url"], "/images/recovered.png")

    def test_statistics_include_active_recovery_and_final_failure(self):
        _, child = self.pending()
        self.runtime.dispatch_once()
        stats = runtime_statistics(self.runtime)
        self.assertEqual(stats["live"]["recovering"], 1)
        self.assertEqual(stats["summary"]["recovering"], 1)
        self.assertIsNone(stats["summary"]["platform_success_rate"])
        self.now += 601
        self.runtime.dispatch_once()
        stats = runtime_statistics(self.runtime)
        self.assertEqual(stats["live"]["recovering"], 0)
        self.assertEqual(stats["summary"]["platform_failures"], 1)
        self.assertEqual(stats["summary"]["platform_success_rate"], 0)


if __name__ == "__main__":
    unittest.main()
