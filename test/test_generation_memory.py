from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from services.generation_runtime import DEFAULT_QUEUE, GenerationRuntime
from services.generation_statistics import runtime_statistics
from services.generation_store import GenerationStore
from services.image_call_logging import runtime_call_details
from services.protocol.conversation import ConversationRequest, ImageGenerationError, ImageOutput


class GenerationMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.runtime = GenerationRuntime(self.directory, executor=mock.Mock())
        self.identity = {"id": "synthetic", "role": "user"}
        self.images = [{"url": "/images/synthetic.png", "b64_json": "A" * 131072,
                        "revised_prompt": "合成测试", "actual_size": "1024x1024"}]

    def tearDown(self):
        self.runtime.store.close()
        self.temp.cleanup()

    def job(self, job_id="synthetic", *, count=1, client_ids=None, finish=True):
        store = self.runtime.store
        store.submit(job_id=job_id, identity=self.identity, endpoint="/v1/images/generations", external_id=job_id,
                     fingerprint=job_id, payload={"model": "gpt-image-2", "client_task_ids": client_ids or []},
                     channel="web", count=count, settings=DEFAULT_QUEUE)
        children = store.rows("SELECT id FROM tasks WHERE job_id=? ORDER BY ordinal", (job_id,))
        if finish:
            for index, child in enumerate(children):
                output = ImageOutput(kind="result", model="gpt-image-2", index=index + 1, total=count, data=self.images)
                store.finish(child["id"], status="success", output=[asdict(output)])
        return children

    def test_statistics_lists_and_diagnostics_never_select_result_body(self):
        self.job(count=2, client_ids=["first", "second"])
        denied = []

        def authorize(action, table, column, _database, _trigger):
            if action == sqlite3.SQLITE_READ and table == "tasks" and column == "output":
                denied.append(column)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.runtime.store.db.set_authorizer(authorize)
        try:
            stats = runtime_statistics(self.runtime)
            listed = self.runtime.jobs(self.identity)
            admin = self.runtime.jobs({"id": "admin"}, all_users=True)
            with mock.patch("services.generation_runtime.get_generation_runtime", return_value=self.runtime):
                diagnostics = runtime_call_details("synthetic", "synthetic")
        finally:
            self.runtime.store.db.set_authorizer(None)
        self.assertEqual(denied, [])
        self.assertEqual(stats["summary"]["successful_images"], 2)
        self.assertEqual([item["id"] for item in listed], ["first", "second"])
        self.assertEqual(len(admin[0]["data"]), 2)
        self.assertEqual(len(diagnostics["image_tasks"]), 2)
        for item in listed:
            self.assertNotIn("b64_json", item["data"][0])
            self.assertEqual(item["data"][0]["revised_prompt"], "合成测试")

    def test_upgrade_backfills_old_bodies_once_and_preserves_base64_delivery(self):
        children = self.job(count=2)
        store = self.runtime.store
        quota = store.quota("synthetic")
        # Simulate a database created before the metadata table existed.
        store.db.execute("DROP TABLE task_output_metadata")
        store.close()
        self.runtime.store = GenerationStore(self.directory / "generation-runtime.sqlite")
        store = self.runtime.store
        statements = []
        store.db.set_trace_callback(lambda query: statements.append(query) if query.startswith("SELECT output") else None)
        self.runtime.jobs(self.identity)
        self.runtime.jobs(self.identity)
        store.db.set_trace_callback(None)
        self.assertEqual(len(statements), 2)
        self.assertEqual(store.quota("synthetic"), quota)
        results = list(self.runtime.outputs("synthetic", ConversationRequest(prompt="test", model="gpt-image-2", n=2)))
        self.assertEqual(len(results), 2)
        self.assertEqual([result.data for result in results], [self.images, self.images])
        self.assertEqual(len(store.rows("SELECT * FROM task_output_metadata")), len(children))

    def test_polling_reads_each_finished_body_only_when_delivering_it(self):
        children = self.job(count=2, finish=False)
        output = asdict(ImageOutput(kind="result", model="gpt-image-2", index=1, total=2, data=self.images))
        self.runtime.store.finish(children[0]["id"], status="success", output=[output])
        iterator = self.runtime.outputs("synthetic", ConversationRequest(prompt="test", model="gpt-image-2", n=2))
        with mock.patch.object(self.runtime.store, "output", wraps=self.runtime.store.output) as read:
            self.assertEqual(next(iterator).data, self.images)
            self.assertEqual(next(iterator).kind, "progress")
            self.runtime.store.finish(children[1]["id"], status="success", output=[output])
            self.assertEqual(next(iterator).data, self.images)
            with self.assertRaises(StopIteration):
                next(iterator)
        self.assertEqual(read.call_count, 2)

    def test_expiry_removes_bodies_but_keeps_history_quota_and_active_checkpoints(self):
        completed = self.job("expired")
        active = self.job("active", count=2, finish=False)
        self.runtime.store.finish(active[0]["id"], status="success", output=[{"kind": "result", "data": self.images}])
        recent = self.job("recent")
        store = self.runtime.store
        store.db.execute("DELETE FROM task_output_metadata WHERE task_id=?", (completed[0]["id"],))
        old = time.time() - 31 * 86400
        store.db.execute("UPDATE jobs SET created=?", (old,))
        store.db.execute("UPDATE tasks SET finished=?,updated=? WHERE job_id IN ('expired','active') AND status='success'", (old, old))
        self.runtime._write_json("expired", "raw.json", {"synthetic": "body"})
        self.runtime._write_json("active", "raw.json", {"synthetic": "body"})
        quota = store.quota("synthetic")
        settlements = store.rows("SELECT * FROM settlements ORDER BY task_id")
        events = store.rows("SELECT * FROM events ORDER BY id")
        self.runtime.cleanup()
        self.runtime.cleanup()
        self.assertIsNone(store.output(completed[0]["id"]))
        self.assertIsNotNone(store.output(active[0]["id"]))
        self.assertIsNotNone(store.output(recent[0]["id"]))
        self.assertFalse((self.directory / "generation-tasks" / "expired").exists())
        self.assertTrue((self.directory / "generation-tasks" / "active" / "raw.json").exists())
        self.assertEqual(store.quota("synthetic"), quota)
        self.assertEqual(store.rows("SELECT * FROM settlements ORDER BY task_id"), settlements)
        self.assertEqual(store.rows("SELECT * FROM events ORDER BY id"), events)
        self.assertEqual(len(store.rows("SELECT id FROM jobs")), 3)
        history = self.runtime.jobs(self.identity, ["expired"])[0]
        self.assertEqual(history["status"], "success")
        self.assertEqual(history["data"][0]["url"], self.images[0]["url"])
        self.assertEqual(runtime_statistics(self.runtime, days=100)["summary"]["successful_images"], 3)
        store.finish(completed[0]["id"], status="success", output=[{"kind": "result", "data": self.images}])
        self.assertEqual(store.quota("synthetic"), quota)
        with self.assertRaises(ImageGenerationError) as expired:
            list(self.runtime.outputs("expired", ConversationRequest(prompt="test", model="gpt-image-2")))
        self.assertEqual((expired.exception.code, expired.exception.status_code), ("image_result_expired", 410))
