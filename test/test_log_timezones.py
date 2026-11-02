from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.system as system
from services.editable_file_task_service import EditableFileTaskService
from services.log_service import LoggedCall, LogService


class LogTimezoneTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.logs = LogService(Path(directory.name) / "logs.jsonl")

    def write_rows(self, *rows: dict):
        self.logs.path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

    def test_new_logs_and_call_details_keep_explicit_utc8_offsets(self):
        start = datetime.fromisoformat("2026-10-29T15:59:59+00:00").timestamp()
        end = datetime.fromisoformat("2026-10-29T16:00:01+00:00").timestamp()
        identity = {"id": "test", "name": "test", "role": "admin"}

        with mock.patch("services.log_service.time.time", return_value=end), \
                mock.patch("services.log_service.log_service", self.logs), \
                mock.patch("services.editable_file_task_service.log_service", self.logs):
            LoggedCall(identity, "/v1/test", "test", "test", started=start).log("完成")
            EditableFileTaskService._log_call(
                object.__new__(EditableFileTaskService), identity, "ppt", start, "",
            )
            self.logs.add("account", "test")

        stored = [json.loads(line) for line in self.logs.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(stored), 3)
        for item in stored:
            self.assertEqual(item["time"], "2026-10-30 00:00:01+08:00")
        for item in stored[:2]:
            self.assertEqual(item["detail"]["started_at"], "2026-10-29 23:59:59+08:00")
            self.assertEqual(item["detail"]["ended_at"], "2026-10-30 00:00:01+08:00")

        # Explicit offsets remain correct even when the server timezone changes later.
        with mock.patch("services.log_service._localize_legacy_time", side_effect=AssertionError("not legacy")):
            items = self.logs.list()
        self.assertEqual([item["time"] for item in items], ["2026-10-30 00:00:01"] * 3)
        self.assertEqual(items[1]["detail"]["started_at"], "2026-10-29 23:59:59")
        self.assertEqual(items[1]["detail"]["ended_at"], "2026-10-30 00:00:01")

    def test_legacy_naive_times_use_server_local_timezone_without_rewriting_file(self):
        self.write_rows({
            "id": "legacy", "type": "call", "time": "2026-10-29 16:00:00",
            "detail": {"started_at": "2026-10-29 15:59:59", "ended_at": "2026-10-29 16:00:00",
                       "request_params": {"started_at": "leave alone"}},
        })
        original = self.logs.path.read_bytes()

        for local_zone, expected in (
            (timezone.utc, ("2026-10-30 00:00:00", "2026-10-29 23:59:59")),
            (timezone(timedelta(hours=8)), ("2026-10-29 16:00:00", "2026-10-29 15:59:59")),
        ):
            with self.subTest(local_zone=local_zone), mock.patch(
                "services.log_service._localize_legacy_time", side_effect=lambda value: value.replace(tzinfo=local_zone),
            ):
                item = self.logs.list()[0]
                self.assertEqual(item["time"], expected[0])
                self.assertEqual(item["detail"]["started_at"], expected[1])
                self.assertEqual(item["detail"]["ended_at"], expected[0])
                self.assertEqual(item["detail"]["request_params"], {"started_at": "leave alone"})
                self.assertEqual(self.logs.list(start_date=expected[0][:10], end_date=expected[0][:10])[0]["id"], "legacy")
        self.assertEqual(self.logs.path.read_bytes(), original)

    def test_unparseable_times_are_preserved(self):
        self.write_rows({
            "id": "invalid", "type": "call", "time": "unknown",
            "detail": {"started_at": "unknown", "ended_at": None},
        })
        item = self.logs.list()[0]
        self.assertEqual(item["time"], "unknown")
        self.assertEqual(item["detail"], {"started_at": "unknown", "ended_at": None})

    def test_logs_api_filters_and_returns_utc8_dates_at_midnight(self):
        self.write_rows(
            {"id": "before", "type": "call", "time": "2026-10-29T15:59:59Z"},
            {"id": "start", "type": "call", "time": "2026-10-29T16:00:00+00:00",
             "detail": {"started_at": "2026-10-29T15:59:59+00:00"}},
            {"id": "end", "type": "call", "time": "2026-10-30T15:59:59Z"},
            {"id": "other-type", "type": "account", "time": "2026-10-30T01:00:00+08:00"},
            {"id": "after", "type": "call", "time": "2026-10-30T16:00:00+00:00"},
        )
        app = FastAPI()
        app.include_router(system.create_router("test"))
        with TestClient(app) as client, mock.patch.object(system, "log_service", self.logs), \
                mock.patch.object(system, "require_admin") as require_admin:
            response = client.get(
                "/api/logs",
                params={"type": "call", "start_date": "2026-10-30", "end_date": "2026-10-30"},
                headers={"Authorization": "Bearer test"},
            )
        self.assertEqual(response.status_code, 200)
        require_admin.assert_called_once_with("Bearer test")
        items = response.json()["items"]
        self.assertEqual([item["id"] for item in items], ["end", "start"])
        self.assertEqual([item["time"] for item in items], ["2026-10-30 23:59:59", "2026-10-30 00:00:00"])
        self.assertEqual(items[1]["detail"]["started_at"], "2026-10-29 23:59:59")


if __name__ == "__main__":
    unittest.main()
