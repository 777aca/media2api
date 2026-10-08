from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import api.support as support_module
import api.system as system_module
import services.config as config_module
import services.image_service as image_module
from services.image_storage_service import ImageStorageService
from services.image_task_service import ImageTaskService


class ImageRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(config_module, "DATA_DIR", self.root).start()
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps({"auth-key": "synthetic-admin", "image_retention_days": 15}), encoding="utf-8")
        self.config = config_module.ConfigStore(self.path)

    def image(self, name: str, age_days: float) -> Path:
        path = self.config.images_dir / "2026" / "10" / "01" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 32), "red").save(path, compress_level=0)
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_partial_save_persists_without_changing_other_settings(self):
        self.config.update({"image_retention_days": 7})
        self.assertEqual(config_module.ConfigStore(self.path).image_retention_days, 7)
        self.assertEqual(json.loads(self.path.read_text())["auth-key"], "synthetic-admin")

    def test_legacy_days_keep_same_duration_without_rewriting_config(self):
        before = self.path.read_bytes()
        self.assertEqual(self.config.image_retention_hours, 360)
        self.assertEqual(self.config.get()["image_retention_hours"], 360)
        self.assertEqual(self.path.read_bytes(), before)

    def test_one_hour_persists_and_takes_precedence_over_legacy_days(self):
        self.config.update({"image_retention_hours": 1, "image_retention_days": 15})
        reloaded = config_module.ConfigStore(self.path)
        self.assertEqual(reloaded.image_retention_hours, 1)
        self.assertAlmostEqual(reloaded.image_retention_days, 1 / 24)
        self.assertEqual(reloaded.get()["image_retention_hours"], 1)
        self.config.update({"image_retention_days": 2})
        self.assertEqual(self.config.image_retention_hours, 48)

    def test_invalid_hours_leave_previous_duration_unchanged(self):
        previous = self.path.read_bytes()
        for value in (0, -1, 0.5, 1.5, "0.5", True, None, "", 876001):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "小时"):
                self.config.update({"image_retention_hours": value, "image_retention_days": 15})
            self.assertEqual(self.config.image_retention_hours, 360)
            self.assertEqual(self.path.read_bytes(), previous)

    def test_one_hour_cleanup_boundary_keeps_images_before_expiry(self):
        path = self.image("hour-boundary.png", 0)
        stamp = path.stat().st_mtime
        self.config.update({"image_retention_hours": 1})
        with mock.patch.object(config_module.time, "time", return_value=stamp + 3599):
            self.assertEqual(self.config.cleanup_old_images(), 0)
            self.assertTrue(path.exists())
        with mock.patch.object(config_module.time, "time", return_value=stamp + 3600):
            self.assertEqual(self.config.cleanup_old_images(), 1)
            self.assertFalse(path.exists())

    def test_invalid_values_do_not_change_persisted_or_live_policy(self):
        previous = self.path.read_bytes()
        for value in (0, -1, True, None, 1.5, 30.0, "", "1.5", "abc", 36501, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "整数"):
                self.config.update({"image_retention_days": value})
            self.assertEqual(self.config.image_retention_days, 15)
            self.assertEqual(self.path.read_bytes(), previous)

    def test_legacy_integer_string_is_normalized(self):
        self.config.update({"image_retention_days": " 7 "})
        self.assertEqual(json.loads(self.path.read_text())["image_retention_days"], 7)

    def test_save_failure_preserves_live_policy(self):
        with mock.patch.object(self.config, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.config.update({"image_retention_days": 1})
        self.assertEqual(self.config.image_retention_days, 15)

    def test_cleanup_uses_updated_days_and_keeps_recent_files_and_logs(self):
        old = self.image("old.png", 10)
        recent = self.image("recent.png", 1)
        log = self.root / "logs.json"
        log.write_text('{"history": "preserved"}', encoding="utf-8")
        self.assertEqual(self.config.cleanup_old_images(), 0)
        self.config.update({"image_retention_days": 7})
        self.assertEqual(self.config.cleanup_old_images(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(log.read_text(), '{"history": "preserved"}')
        self.assertEqual(self.config.cleanup_old_images(), 0)

    def test_exact_expiry_is_cleaned(self):
        path = self.image("boundary.png", 0)
        stamp = path.stat().st_mtime
        with mock.patch.object(config_module.time, "time", return_value=stamp + 15 * 86400):
            self.assertEqual(self.config.cleanup_old_images(), 1)

    def test_list_removes_expired_local_image_and_thumbnail_but_keeps_remote_copy(self):
        local = self.image("local.png", 20)
        both = self.image("both.png", 20)
        local_rel = local.relative_to(self.config.images_dir).as_posix()
        both_rel = both.relative_to(self.config.images_dir).as_posix()
        for rel in (local_rel, both_rel):
            thumbnail = self.config.image_thumbnails_dir / f"{rel}.png"
            thumbnail.parent.mkdir(parents=True, exist_ok=True)
            thumbnail.write_bytes(b"thumbnail")
        storage = ImageStorageService(self.root / "image_index.json")
        storage._save_index({both_rel: {"webdav": True, "local": True, "date": "2026-10-01"}})
        with (
            mock.patch.object(image_module, "config", self.config),
            mock.patch.object(image_module, "load_tags", return_value={}),
            mock.patch.object(image_module, "image_storage_service", storage),
            mock.patch("services.image_storage_service.config", self.config),
            mock.patch("services.image_storage_service.WebDAVClient") as remote,
        ):
            result = image_module.list_images("http://example.test")
        self.assertEqual([item["path"] for item in result["items"]], [both_rel])
        self.assertEqual(result["items"][0]["storage"], "webdav")
        self.assertFalse(local.exists())
        self.assertFalse(both.exists())
        self.assertFalse((self.config.image_thumbnails_dir / f"{local_rel}.png").exists())
        self.assertTrue((self.config.image_thumbnails_dir / f"{both_rel}.png").exists())
        remote.assert_not_called()

    def test_compression_does_not_extend_expiry(self):
        old = self.image("compress.png", 20)
        stamp = old.stat().st_mtime_ns
        with mock.patch.object(image_module, "config", self.config):
            result = image_module.compress_images()
        self.assertEqual(result["compressed"], 1)
        self.assertEqual(old.stat().st_mtime_ns, stamp)
        self.assertEqual(self.config.cleanup_old_images(), 1)

    def test_records_survive_shortening_file_retention_and_restart(self):
        tasks = self.root / "image_tasks.json"
        tasks.write_text(json.dumps({"tasks": [{
            "id": "old-task", "owner_id": "owner", "status": "success", "model": "gpt-image-2",
            "created_at": "2000-01-01 00:00:00", "updated_at": "2000-01-01 00:00:00",
            "data": [{"url": "http://example.test/expired.png"}],
        }]}), encoding="utf-8")
        self.config.update({"image_retention_hours": 1})
        with mock.patch("services.image_task_service.config", self.config):
            for _ in range(2):
                service = ImageTaskService(tasks)
                result = service.list_tasks({"id": "owner", "role": "admin"}, ["old-task"])
                self.assertEqual(result["missing_ids"], [])
                self.assertEqual(result["items"][0]["status"], "success")

    def test_scheduler_cleans_with_current_policy(self):
        old = self.image("scheduled.png", 10)
        stop = mock.Mock()
        calls = 0

        def wait(_seconds):
            self.assertEqual(_seconds, 60)
            nonlocal calls
            calls += 1
            if calls == 2:
                self.config.update({"image_retention_hours": 1})
            return calls > 2

        stop.wait.side_effect = wait
        with (
            mock.patch.object(image_module, "config", self.config),
            mock.patch.object(image_module, "cleanup_image_thumbnails"),
            mock.patch.object(image_module.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**3)),
        ):
            image_module._auto_cleanup_worker(stop)
        self.assertFalse(old.exists())

    def test_settings_api_validates_and_requires_admin(self):
        mock.patch.object(system_module, "config", self.config).start()
        mock.patch.object(support_module, "config", SimpleNamespace(auth_key="synthetic-admin")).start()
        mock.patch.object(support_module.auth_service, "authenticate", side_effect=lambda key: {
            "id": "user", "role": "user"
        } if key == "synthetic-user" else None).start()
        app = FastAPI()
        app.include_router(system_module.create_router("test"))
        with TestClient(app) as client:
            for key, expected in ((None, 401), ("synthetic-user", 403)):
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                response = client.post("/api/settings", json={"image_retention_days": 1}, headers=headers)
                self.assertEqual(response.status_code, expected)
            headers = {"Authorization": "Bearer synthetic-admin"}
            response = client.post("/api/settings", json={"image_retention_days": 0}, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.config.image_retention_days, 15)
            response = client.post("/api/settings", json={"image_retention_days": 7}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/settings", headers=headers).json()["config"]["image_retention_days"], 7)
            for value in (0, 0.5, -1):
                response = client.post("/api/settings", json={"image_retention_hours": value}, headers=headers)
                self.assertEqual(response.status_code, 400)
            response = client.post("/api/settings", json={"image_retention_hours": 1}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/settings", headers=headers).json()["config"]["image_retention_hours"], 1)
            self.assertEqual(config_module.ConfigStore(self.path).image_retention_hours, 1)


if __name__ == "__main__":
    unittest.main()
