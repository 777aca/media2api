from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from services.log_service import LogService


class LegacyImageCallLoggingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.logs = LogService(Path(directory.name) / "logs.jsonl")
        self.detail = {"key_id": "synthetic-owner", "endpoint": "/v1/images/generations", "image_task_id": "job-1"}

    def status(self, **detail):
        self.logs.add("call", "生图任务状态", {**self.detail, "image_child_id": "child-1",
                      "phase": "output_saved", "retry_count": 1, "outcome": "success", **detail})

    def call(self, **detail):
        self.logs.add("call", "文生图调用完成", {**self.detail, "status": "success", "duration_ms": 1234,
                      "urls": ["/images/synthetic.png"], "request_params": {"prompt": "draw"}, **detail})

    def test_historical_pairs_merge_before_limit_without_rewriting_history(self):
        self.call(image_task_id="older")
        self.status()
        self.call()
        before = self.logs.path.read_bytes()
        items = self.logs.list(type="call", limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual([item["summary"] for item in items], ["文生图调用完成"] * 2)
        detail = items[0]["detail"]
        self.assertEqual(detail["duration_ms"], 1234)
        self.assertEqual(detail["urls"], ["/images/synthetic.png"])
        self.assertEqual(detail["request_params"], {"prompt": "draw"})
        self.assertEqual(detail["retry_count"], 1)
        self.assertEqual(detail["image_tasks"][0]["image_child_id"], "child-1")
        self.assertEqual(self.logs.path.read_bytes(), before)

    def test_all_children_merge_and_latest_diagnostic_wins_even_if_written_after_call(self):
        self.status(phase="rejected", outcome="queued")
        self.call()
        self.status()
        self.status(image_child_id="child-2", outcome="error", error_category="transient")
        items = self.logs.list()
        self.assertEqual(len(items), 1)
        children = {child["image_child_id"]: child for child in items[0]["detail"]["image_tasks"]}
        self.assertEqual(children["child-1"]["outcome"], "success")
        self.assertEqual(children["child-2"]["error_category"], "transient")

    def test_distinct_calls_and_orphan_or_web_task_logs_are_preserved(self):
        self.status()
        self.call()
        self.call()  # A separate idempotent HTTP read remains a real call.
        self.status(key_id="other-owner")
        self.status(image_task_id="different-job")
        self.status(endpoint="/api/image-tasks/generations")
        self.call(image_task_id="")
        self.assertEqual(len(self.logs.list(type="call")), 6)

    def test_date_and_type_filters_still_apply_to_the_actual_call(self):
        self.status()
        self.call()
        rows = [json.loads(line) for line in self.logs.path.read_text(encoding="utf-8").splitlines()]
        rows[0]["time"] = "2026-10-08 23:59:59"
        rows[1]["time"] = "2026-10-09 00:00:00"
        self.logs.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        self.assertEqual(len(self.logs.list(start_date="2026-10-09", end_date="2026-10-09")), 1)
        self.assertEqual(self.logs.list(end_date="2026-10-08"), [])
        self.assertEqual(self.logs.list(type="account"), [])

    def test_deleting_visible_call_also_removes_its_hidden_diagnostics(self):
        self.status(phase="rejected", outcome="queued")
        self.status()
        self.call()
        self.status(image_task_id="unrelated")
        target = next(item for item in self.logs.list() if item["summary"] == "文生图调用完成")
        self.logs.delete([target["id"]])
        remaining = self.logs.list()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["detail"]["image_task_id"], "unrelated")


if __name__ == "__main__":
    unittest.main()
