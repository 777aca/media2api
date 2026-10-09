from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from services.generation_context import GenerationContext, checkpoint
from services.generation_errors import GenerationRuntimeError, classify_image_error
from services.generation_runtime import GenerationRuntime, DEFAULT_QUEUE
from services.generation_statistics import runtime_statistics
from services.generation_store import GenerationStore
from services.protocol.conversation import ConversationRequest, ImageOutput
from utils.helper import UpstreamHTTPError


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.identities = {key: {"id": key, "role": "user", "enabled": True} for key in ("a", "b", "c")}
        self.settings = dict(DEFAULT_QUEUE)
        self.release = threading.Event()
        self.runtime = GenerationRuntime(self.directory, settings_getter=lambda: self.settings, identity_resolver=self.identities.get,
                                         executor=self.fake_execute)

    def tearDown(self):
        self.release.set()
        self.runtime.stop()
        self.runtime.store.close()
        self.temp.cleanup()

    def fake_execute(self, request, index, total, row):
        self.release.wait(5)
        return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": f"/images/{row['id']}.png"}])]

    def submit(self, owner="a", n=1, key="", prompt="test"):
        context = GenerationContext(self.identities[owner], "/v1/images/generations", key)
        return self.runtime.submit(ConversationRequest(model="gpt-image-2", prompt=prompt, n=n), context)

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() > deadline:
                self.fail("runtime did not reach expected state")
            time.sleep(.02)

    def test_global_and_key_concurrency_and_fifo(self):
        self.submit(n=8)
        self.submit("b", n=8)
        self.wait_for(lambda: len(self.runtime._active) == 8)
        self.assertEqual(list(self.runtime._active.values()).count("a"), 4)
        self.assertEqual(list(self.runtime._active.values()).count("b"), 4)
        for owner in ("a", "b"):
            ordinals = self.runtime.store.rows("SELECT ordinal FROM tasks WHERE owner=? AND status='running' ORDER BY ordinal", (owner,))
            self.assertEqual([r["ordinal"] for r in ordinals], [1, 2, 3, 4])

    def test_atomic_reservations_and_idempotency(self):
        self.runtime.store.set_limits("a", {"image_quota_limit": 5})
        def submit(_):
            try:
                return self.submit(n=3)
            except GenerationRuntimeError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(results.count("image_quota_exceeded"), 7)
        self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 3)
        first = self.submit("b", n=2, key="repeat")
        self.assertEqual(self.submit("b", n=2, key="repeat"), first)
        with self.assertRaises(GenerationRuntimeError) as conflict:
            self.submit("b", n=2, key="repeat", prompt="changed")
        self.assertEqual(conflict.exception.status_code, 409)
        self.release.set()
        self.wait_for(lambda: self.runtime.store.quota("a")["image_quota_used"] == 3)
        self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 0)

    def test_queue_capacity_cancel_and_disabled_key(self):
        self.settings.update(global_concurrency=1, key_concurrency=1, max_waiting_images=2)
        self.submit()
        self.wait_for(lambda: len(self.runtime._active) == 1)
        second = self.submit(n=2)
        with self.assertRaises(GenerationRuntimeError) as full:
            self.submit("b")
        self.assertEqual(full.exception.code, "image_queue_full")
        self.runtime.change_task(self.identities["a"], second, "cancel")
        self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 1)
        self.submit("b", n=2)
        self.identities["b"]["enabled"] = False
        self.wait_for(lambda: self.runtime.store.quota("b")["image_quota_reserved"] == 0)

    def test_queue_timeout(self):
        self.settings.update(global_concurrency=1, key_concurrency=1)
        self.submit()
        self.wait_for(lambda: len(self.runtime._active) == 1)
        second = self.submit("b")
        with self.runtime.store.lock:
            self.runtime.store.db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, second))
        self.wait_for(lambda: self.runtime.store.quota("b")["image_quota_reserved"] == 0)
        self.assertEqual(self.runtime.jobs(self.identities["b"], [second])[0]["error_code"], "image_queue_timeout")

    def test_exact_default_waiting_boundary_is_200_images(self):
        self.runtime.store.submit(job_id="capacity", identity=self.identities["a"], endpoint="test", external_id="", fingerprint="capacity",
                                  payload={"model": "gpt-image-2"}, channel="web", count=200, settings=self.settings)
        with self.assertRaises(GenerationRuntimeError) as full:
            self.runtime.store.submit(job_id="overflow", identity=self.identities["b"], endpoint="test", external_id="", fingerprint="overflow",
                                      payload={"model": "gpt-image-2"}, channel="web", count=1, settings=self.settings)
        self.assertEqual(full.exception.code, "image_queue_full")
        self.assertEqual(self.runtime.store.quota("b")["image_quota_reserved"], 0)

    def test_round_robin_with_keys_arriving_while_first_key_is_running(self):
        self.settings.update(global_concurrency=1, key_concurrency=1)
        order = []
        gate = threading.Semaphore(0)
        def execute(request, index, total, row):
            order.append(row["owner"])
            gate.acquire(timeout=3)
            return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "/image"}])]
        self.runtime.execute = execute
        self.submit("a", n=2)
        self.wait_for(lambda: len(order) == 1)
        self.submit("b", n=2)
        self.submit("c")
        for count in range(2, 6):
            gate.release()
            self.wait_for(lambda: len(order) == count)
        gate.release()
        self.assertEqual(order, ["a", "b", "c", "a", "b"])

    def test_settlement_extras_and_duplicates(self):
        self.runtime.store.set_limits("a", {"image_quota_limit": 2})
        job = self.submit(n=2)
        tasks = self.runtime.store.rows("SELECT id FROM tasks WHERE job_id=?", (job,))
        # First task cannot spend the second task's reserved picture.
        result = [{"kind": "result", "data": [{"url": "/a"}, {"url": "/b"}, {"url": "/c"}]}]
        delivered = self.runtime.store.finish(tasks[0]["id"], status="success", output=result)
        self.assertEqual(len(delivered[0]["data"]), 1)
        self.runtime.store.finish(tasks[0]["id"], status="success", output=result)
        self.runtime.store.finish(tasks[1]["id"], status="error", category="invalid_request")
        self.assertEqual(self.runtime.store.quota("a")["image_quota_used"], 1)
        self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 0)
        with self.assertRaises(ValueError):
            self.runtime.store.set_limits("a", {"image_quota_limit": 0})

    def test_submission_uncertainty_keeps_reservation_and_admin_can_end(self):
        calls = []
        def uncertain(request, index, total, row):
            calls.append(row["id"])
            checkpoint("submitting")
            raise TimeoutError("read timed out")
        self.runtime.execute = uncertain
        job = self.submit()
        self.wait_for(lambda: self.runtime.jobs(self.identities["a"], [job])[0]["status"] == "uncertain")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 1)
        with self.assertRaises(GenerationRuntimeError):
            self.runtime.change_task(self.identities["a"], job, "end")
        self.runtime.change_task({"id": "admin", "role": "admin"}, job, "end")
        self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 0)
        self.assertEqual(self.runtime.jobs(self.identities["b"], [job]), [])

    def test_checkpoint_restart_and_consistent_snapshot(self):
        # No live worker may mutate the handcrafted restart fixture.
        self.runtime.start()
        self.runtime.stop()
        for phase in ("preparing", "account_selected", "submitting", "submitted", "raw_saved", "output_saved"):
            job_id = phase.replace("_", "")
            self.runtime.store.submit(job_id=job_id, identity=self.identities["a"], endpoint="test", external_id="", fingerprint=phase,
                                      payload={"model": "gpt-image-2"}, channel="web", count=1, settings=self.settings)
            child = self.runtime.store.rows("SELECT id FROM tasks WHERE job_id=?", (job_id,))[0]["id"]
            self.runtime.store.claim(child)
            self.runtime.store.checkpoint(child, phase, conversation_id="conversation" if phase == "submitted" else "")
        self.runtime.store.recover()
        rows = {row["phase"]: row for row in self.runtime.store.rows("SELECT * FROM tasks")}
        self.assertEqual(rows["submitting"]["status"], "uncertain")
        for phase in ("submitted", "raw_saved", "output_saved"):
            self.assertEqual(rows[phase]["recovery"], "recovering_result")
        self.assertEqual(rows["preparing"]["status"], "queued")
        snapshot = self.directory / "snapshot.sqlite"
        self.runtime.store.snapshot(snapshot)
        restored = GenerationStore(snapshot)
        self.assertEqual(restored.quota("a")["image_quota_reserved"], 6)
        restored.close()

    def test_single_instance_lease(self):
        self.runtime.start()
        second = GenerationRuntime(self.directory)
        try:
            with self.assertRaises(RuntimeError):
                second.start()
        finally:
            second.store.close()

    def test_statistics_excludes_user_faults_from_platform_denominator(self):
        self.assertIsNone(runtime_statistics(self.runtime)["summary"]["platform_success_rate"])
        self.runtime.execute = lambda request, index, total, row: (_ for _ in ()).throw(GenerationRuntimeError("bad parameter", "invalid_image_request", 400))
        job = self.submit()
        self.wait_for(lambda: self.runtime.jobs(self.identities["a"], [job])[0]["status"] == "error")
        stats = runtime_statistics(self.runtime)["summary"]
        self.assertEqual(stats["invalid_request"], 1)
        self.assertIsNone(stats["platform_success_rate"])


class ClassificationTests(unittest.TestCase):
    def test_structured_policy(self):
        cases = [(401, {}, "credentials_invalid", 0), (403, {}, "access_denied", 300),
                 (403, {"error": {"code": "model_not_found"}}, "model_permission", 0),
                 (429, {}, "rate_limited", 180), (503, {}, "transient", 30),
                 (400, {}, "invalid_request", 0), (400, {"error": {"code": "content_policy_violation"}}, "content_rejected", 0)]
        for status, body, category, cooldown in cases:
            with self.subTest(status=status, body=body):
                result = classify_image_error(UpstreamHTTPError("test", status, body))
                self.assertEqual((result.category, result.cooldown_seconds), (category, cooldown))
        self.assertEqual(classify_image_error(TimeoutError(), consecutive=10).cooldown_seconds, 900)
        self.assertEqual(classify_image_error(UpstreamHTTPError("test", 429, {}, retry_after=99999999)).cooldown_seconds, 604800)
        self.assertEqual(classify_image_error(UpstreamHTTPError("test", 429, {}, retry_after=1)).cooldown_seconds, 60)
        self.assertEqual(classify_image_error(UpstreamHTTPError("test", 429, {"error": {"reset_at": "1970-01-01T08:02:00+08:00"}}), now=0).cooldown_seconds, 120)


if __name__ == "__main__":
    unittest.main()
