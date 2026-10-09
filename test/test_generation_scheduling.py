from __future__ import annotations

import json
import threading
import time
from unittest import mock

from services.generation_context import GenerationContext, checkpoint
from services.generation_errors import GenerationRuntimeError
from services.generation_runtime import DEFAULT_QUEUE, GenerationRuntime
from services.generation_statistics import runtime_statistics
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, ImageOutput
from test.test_account_scheduling import SchedulingTestCase
from utils.helper import UpstreamHTTPError


class UnifiedSchedulingTests(SchedulingTestCase):
    def setUp(self):
        super().setUp()
        self.identities = {key: {"id": key, "role": "user", "enabled": True} for key in ("a", "b")}
        self.settings = dict(DEFAULT_QUEUE)
        self.gate = threading.Event()
        self.calls = []
        self.runtime = GenerationRuntime(self.path, settings_getter=lambda: self.settings,
                                         identity_resolver=self.identities.get, account_pool=self.service, executor=self.execute)
        self.addCleanup(self.runtime.store.close)
        self.addCleanup(self.runtime.stop)
        self.addCleanup(self.gate.set)

    def execute(self, request, index, total, row):
        self.calls.append((row["owner"], row["id"], row["_account_token"]))
        self.gate.wait(3)
        return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "/synthetic.png"}])]

    def submit(self, owner="a", model="gpt-image-2", n=1):
        return self.runtime.submit(ConversationRequest(model=model, prompt="synthetic", n=n), GenerationContext(self.identities[owner], "test"))

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("scheduler did not reach expected state")
            time.sleep(.01)

    def state(self, job):
        return self.runtime.store.rows("SELECT * FROM tasks WHERE job_id=? ORDER BY ordinal", (job,))

    def test_busy_account_remains_queued_and_cancellable_without_using_worker_slots(self):
        self.add("busy")
        token = self.service.try_acquire_image_token(model="gpt-image-2", channel="web", excluded_ids=set())
        try:
            self.settings["max_waiting_images"] = 2
            job = self.submit(n=2)
            self.runtime.dispatch_once()
            self.assertEqual(self.runtime._active, {})
            self.assertEqual(self.calls, [])
            self.assertTrue(all(row["started"] is None and row["status"] == "queued" for row in self.state(job)))
            live = runtime_statistics(self.runtime)["live"]
            self.assertEqual((live["running"], live["queued"]), (0, 2))
            with self.assertRaises(GenerationRuntimeError) as full:
                self.submit("b")
            self.assertEqual(full.exception.code, "image_queue_full")
            self.runtime.change_task(self.identities["a"], job, "cancel")
            self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 0)
        finally:
            self.service.release_image_slot(token)

    def test_busy_head_preserves_key_fifo_without_blocking_another_key_and_model(self):
        self.add("free", type="pro", source_type="codex")
        self.add("codex", type="plus", source_type="codex")
        token = self.service.try_acquire_image_token(model="pro-codex-gpt-image-2", channel="codex", excluded_ids=set())
        self.settings["global_concurrency"] = 1
        try:
            first = self.submit(model="pro-codex-gpt-image-2")
            second = self.submit(model="plus-codex-gpt-image-2")
            other = self.submit("b", model="plus-codex-gpt-image-2")
            self.wait_for(lambda: len(self.calls) == 1)
            self.assertEqual(self.calls[0][0], "b")
            self.assertEqual(self.state(first)[0]["status"], "queued")
            self.assertEqual(self.state(second)[0]["status"], "queued")
            self.gate.set()
            self.wait_for(lambda: self.state(other)[0]["status"] == "success")
        finally:
            self.service.release_image_slot(token)
        self.wait_for(lambda: self.state(second)[0]["status"] == "success")
        self.assertEqual([owner for owner, _, _ in self.calls], ["b", "a", "a"])

    def test_account_slot_is_held_through_processing_and_released_once(self):
        self.add("one")
        job = self.submit(n=3)
        self.wait_for(lambda: len(self.calls) == 1)
        self.assertEqual([row["status"] for row in self.state(job)], ["running", "queued", "queued"])
        self.assertEqual(len(self.runtime._active), 1)
        self.assertEqual(self.service.list_accounts()[0]["image_inflight"], 1)
        self.gate.set()
        self.wait_for(lambda: len(self.calls) == 3 and not self.runtime._active)
        self.assertEqual(self.service.list_accounts()[0]["image_inflight"], 0)
        self.assertEqual(self.runtime.store.quota("a")["image_quota_used"], 3)

    def test_safe_retry_rejoins_queue_and_remembers_failed_account_across_restart(self):
        self.add("first", type="plus", source_type="codex", priority=10)
        self.add("second", type="plus", source_type="codex", priority=5)
        self.add("free", type="pro", source_type="codex")
        first_id = self.service.get_account("first")["pool_account_id"]
        occupied = self.service.try_acquire_image_token(model="plus-codex-gpt-image-2", channel="codex", excluded_ids={first_id})
        self.settings["global_concurrency"] = 1
        self.runtime.execute = self.runtime._execute_image
        attempts = []
        def output(backend, request, index, total):
            attempts.append(backend.access_token)
            checkpoint("submitting")
            if backend.access_token == "first":
                raise UpstreamHTTPError("generation", 429, {})
            return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "/synthetic.png"}])]
        def backend(access_token):
            return mock.Mock(access_token=access_token)
        with mock.patch.object(conversation, "OpenAIBackendAPI", side_effect=backend), mock.patch.object(conversation, "stream_codex_image_outputs", side_effect=output), mock.patch.object(conversation, "stream_image_outputs", side_effect=output):
            try:
                job = self.submit(model="plus-codex-gpt-image-2")
                self.wait_for(lambda: self.state(job)[0]["recovery"] == "retrying_account" and not self.runtime._active)
                self.assertEqual(json.loads(self.state(job)[0]["attempted_accounts"]), [first_id])
                self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 1)
                other = self.submit("b", model="pro-codex-gpt-image-2")
                self.wait_for(lambda: self.state(other)[0]["status"] == "success")
                self.runtime.stop()
                self.runtime.start()
                self.assertEqual(self.state(job)[0]["status"], "queued")
            finally:
                self.service.release_image_slot(occupied)
            self.wait_for(lambda: self.state(job)[0]["status"] == "success" and not self.runtime._active)
        self.assertEqual(attempts, ["first", "free", "second"])
        self.assertEqual(self.state(job)[0]["attempt"], 2)
        self.assertEqual(self.runtime.store.quota("a")["image_quota_used"], 1)
        self.assertTrue(all(account["image_inflight"] == 0 for account in self.service.list_accounts()))

    def test_waiting_for_account_timeout_refunds_reservation(self):
        self.add("one")
        token = self.service.try_acquire_image_token(model="gpt-image-2", channel="web", excluded_ids=set())
        try:
            job = self.submit()
            with self.runtime.store.lock:
                self.runtime.store.db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, job))
            self.wait_for(lambda: self.state(job)[0]["status"] == "error")
            self.assertEqual(self.state(job)[0]["error_code"], "image_queue_timeout")
            self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 0)
            self.assertEqual(self.calls, [])
        finally:
            self.service.release_image_slot(token)

    def test_cooldown_waits_in_same_queue_and_expiry_restores_scheduling(self):
        self.add("one")
        self.service.update_account("one", {"image_blocks": {"web": {"reason": "rate_limited", "until": time.time() + 300}}}, quiet=True)
        job = self.submit()
        self.runtime.dispatch_once()
        self.assertEqual(self.state(job)[0]["status"], "queued")
        self.assertEqual(self.runtime._active, {})
        self.service.update_account("one", {"image_blocks": {"web": {"reason": "rate_limited", "until": time.time() - 1}}}, quiet=True)
        self.gate.set()
        self.wait_for(lambda: self.state(job)[0]["status"] == "success" and not self.runtime._active)
        self.assertEqual(len(self.calls), 1)

    def test_durable_retry_budget_is_three_distinct_accounts(self):
        for index in range(4):
            self.add(f"account-{index}", type="plus", source_type="codex")
        self.runtime.execute = self.runtime._execute_image
        attempts = []
        def reject(backend, *_args):
            attempts.append(backend.access_token)
            checkpoint("submitting")
            raise UpstreamHTTPError("generation", 429, {})
        with mock.patch.object(conversation, "OpenAIBackendAPI", side_effect=lambda access_token: mock.Mock(access_token=access_token)), mock.patch.object(conversation, "stream_codex_image_outputs", side_effect=reject):
            job = self.submit(model="codex-gpt-image-2")
            self.wait_for(lambda: self.state(job)[0]["status"] == "error" and not self.runtime._active)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(set(attempts)), 3)
        self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 0)
        self.assertEqual(self.runtime.store.quota("a")["image_quota_used"], 0)
        self.assertTrue(all(account["image_inflight"] == 0 for account in self.service.list_accounts()))

    def test_cancel_race_releases_account_when_task_cannot_be_claimed(self):
        self.add("one")
        with mock.patch.object(self.runtime, "start"):
            self.submit()
        with mock.patch.object(self.runtime.store, "claim", return_value=False):
            self.runtime.dispatch_once()
        self.assertEqual(self.runtime._active, {})
        self.assertEqual(self.service.list_accounts()[0]["image_inflight"], 0)

    def test_worker_handoff_failure_releases_all_slots_and_preserves_queue(self):
        self.add("one")
        with mock.patch.object(self.runtime, "start"):
            job = self.submit()
        with mock.patch.object(self.runtime, "_pool") as pool:
            pool.submit.side_effect = RuntimeError("executor unavailable")
            with self.assertRaisesRegex(RuntimeError, "executor unavailable"):
                self.runtime.dispatch_once()
        self.assertEqual(self.runtime._active, {})
        self.assertEqual(self.service.list_accounts()[0]["image_inflight"], 0)
        self.assertEqual(self.state(job)[0]["status"], "queued")
        self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 1)
        self.assertIsNone(self.state(job)[0]["started"])

    def test_restart_after_third_rejection_does_not_choose_a_fourth_account(self):
        for index in range(4):
            self.add(f"account-{index}")
        with mock.patch.object(self.runtime, "start"):
            job = self.submit()
        child = self.state(job)[0]["id"]
        attempted = [account["pool_account_id"] for account in self.service.list_accounts()[:3]]
        self.runtime.store.claim(child)
        self.runtime.store.checkpoint(child, "rejected", attempted_accounts=json.dumps(attempted), attempt=3, error_category="rate_limited", error_code="upstream_rate_limit", error="上游生图限流", http_status=429)
        self.runtime.start()
        self.wait_for(lambda: self.state(job)[0]["status"] == "error")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runtime.store.quota("a")["image_quota_reserved"], 0)
        self.assertEqual(self.state(job)[0]["attempt"], 3)
