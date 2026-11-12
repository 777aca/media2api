from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from datetime import datetime
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import api.support as support_module
import api.system as system_module
import services.config as config_module
import services.image_service as image_module
import services.image_storage_service as storage_module
from services.image_storage_service import ImageStorageError, ImageStorageService
from test.legacy_image_task_fixture import ImageTaskService


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
        mock.patch.object(storage_module, "config", self.config).start()
        self.storage = ImageStorageService(self.root / "image_index.json")

    def remote_image(self, name: str, age_hours: float, *, local: bool = False) -> str:
        rel = f"2026/10/01/{name}"
        if local:
            self.image(name, age_hours / 24)
        items = self.storage._load_index()
        items[rel] = {
            "s3": True, "local": local, "storage": "both" if local else "s3",
            "created_at": datetime.fromtimestamp(time.time() - age_hours * 3600, storage_module.IMAGE_TIMEZONE).isoformat(),
        }
        self.storage._save_index(items)
        return rel

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
        with mock.patch.object(storage_module.time, "time", return_value=stamp + 3599):
            self.assertEqual(self.config.cleanup_old_images(), 0)
            self.assertTrue(path.exists())
        with mock.patch.object(storage_module.time, "time", return_value=stamp + 3600):
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
        with mock.patch.object(storage_module.time, "time", return_value=stamp + 15 * 86400):
            self.assertEqual(self.config.cleanup_old_images(), 1)

    def test_remote_only_r2_expires_at_exact_hour_and_keeps_recent_images(self):
        self.config.update({"image_retention_hours": 1})
        stamp = int(time.time())
        with mock.patch.object(storage_module.time, "time", return_value=stamp):
            expired = self.remote_image("expired.png", 1)
            recent = self.remote_image("recent.png", 3599 / 3600)
            with mock.patch.object(storage_module, "S3ImageClient") as client:
                self.assertEqual(self.config.cleanup_old_images(), 1)
                client.return_value.delete.assert_called_once_with(expired)
        self.assertEqual(list(self.storage._load_index()), [recent])

    def test_r2_both_copies_expire_with_index_and_thumbnail(self):
        self.config.update({"image_retention_hours": 1})
        rel = self.remote_image("both.png", 2, local=True)
        thumbnail = self.config.image_thumbnails_dir / f"{rel}.png"
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        thumbnail.write_bytes(b"thumbnail")
        with (
            mock.patch.object(image_module, "config", self.config),
            mock.patch.object(image_module, "image_storage_service", self.storage),
            mock.patch.object(image_module, "load_tags", return_value={}),
            mock.patch.object(storage_module, "S3ImageClient") as client,
        ):
            self.assertEqual(image_module.list_images("http://example.test")["items"], [])
        client.return_value.delete.assert_called_once_with(rel)
        self.assertFalse((self.config.images_dir / rel).exists())
        self.assertFalse(thumbnail.exists())
        self.assertEqual(self.storage._load_index(), {})

    def test_r2_failure_keeps_local_index_and_thumbnail_then_retries(self):
        self.config.update({"image_retention_hours": 1})
        failed = self.remote_image("failed.png", 2, local=True)
        other = self.remote_image("other.png", 2)
        thumbnail = self.config.image_thumbnails_dir / f"{failed}.png"
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        thumbnail.write_bytes(b"thumbnail")

        def delete(rel):
            if rel == failed:
                raise ImageStorageError("R2/S3 DELETE failed: HTTP 503")
            return True

        with mock.patch.object(storage_module, "S3ImageClient") as client:
            client.return_value.delete.side_effect = delete
            self.assertEqual(self.config.cleanup_old_images(), 1)
            self.assertEqual(client.return_value.delete.call_count, 2)
            self.assertTrue((self.config.images_dir / failed).exists())
            self.assertTrue(thumbnail.exists())
            self.assertEqual(list(self.storage._load_index()), [failed])
            self.assertNotIn(other, self.storage._load_index())
            client.return_value.delete.side_effect = None
            client.return_value.delete.return_value = True
            self.assertEqual(self.config.cleanup_old_images(), 1)
        self.assertFalse((self.config.images_dir / failed).exists())
        self.assertEqual(self.storage._load_index(), {})

    def test_r2_already_missing_still_removes_index(self):
        self.config.update({"image_retention_hours": 1})
        self.remote_image("missing.png", 2)
        with mock.patch.object(storage_module, "S3ImageClient") as client:
            client.return_value.delete.return_value = False  # HTTP 404
            self.assertEqual(self.config.cleanup_old_images(), 1)
            self.assertEqual(self.config.cleanup_old_images(), 0)
            client.return_value.delete.assert_called_once()
        self.assertEqual(self.storage._load_index(), {})

    def test_legacy_remote_time_falls_back_to_filename_and_skips_unknown(self):
        self.config.update({"image_retention_hours": 1})
        expired = f"2026/10/01/{int(time.time() - 7200)}_hash.png"
        unknown = "2026/10/01/unknown.png"
        self.storage._save_index({rel: {"s3": True} for rel in (expired, unknown)})
        with mock.patch.object(storage_module, "S3ImageClient") as client:
            self.assertEqual(self.config.cleanup_old_images(), 1)
            client.return_value.delete.assert_called_once_with(expired)
        self.assertEqual(list(self.storage._load_index()), [unknown])

    def test_r2_expiry_keeps_webdav_copy(self):
        self.config.update({"image_retention_hours": 1})
        rel = self.remote_image("migrated.png", 2, local=True)
        items = self.storage._load_index()
        items[rel]["webdav"] = True
        self.storage._save_index(items)
        with (
            mock.patch.object(storage_module, "S3ImageClient") as s3,
            mock.patch.object(storage_module, "WebDAVClient") as webdav,
        ):
            self.assertEqual(self.config.cleanup_old_images(), 1)
            s3.return_value.delete.assert_called_once_with(rel)
            webdav.assert_not_called()
            self.assertEqual(self.config.cleanup_old_images(), 0)
        item = self.storage._load_index()[rel]
        self.assertTrue(item["webdav"])
        self.assertFalse(item["s3"])
        self.assertFalse(item["local"])

    def test_saving_new_image_cleans_remote_images_using_same_index(self):
        self.config.update({"image_retention_hours": 1})
        expired = self.remote_image("old.png", 2)
        with mock.patch.object(storage_module, "S3ImageClient") as client:
            stored = self.storage.save(b"synthetic-image")
            client.return_value.delete.assert_called_once_with(expired)
        self.assertEqual(list(self.storage._load_index()), [stored.rel])

    def test_remote_timeout_is_retained_and_other_expired_images_are_cleaned(self):
        self.config.update({"image_retention_hours": 1})
        failed = self.remote_image("timeout.png", 2)
        local = self.image("local-after-timeout.png", 2 / 24)
        with (
            mock.patch.object(storage_module, "S3ImageClient") as client,
            mock.patch.object(storage_module.logger, "warning") as warning,
        ):
            client.return_value.delete.side_effect = TimeoutError("synthetic timeout")
            self.assertEqual(self.config.cleanup_old_images(), 1)
            warning.assert_called_once_with({"event": "image_retention_delete_failed", "path": failed,
                                             "error_type": "TimeoutError"})
        self.assertFalse(local.exists())
        self.assertIn(failed, self.storage._load_index())

    def test_manual_delete_api_reports_r2_failure_and_keeps_image(self):
        rel = self.remote_image("manual.png", 2, local=True)
        app = FastAPI()
        app.include_router(system_module.create_router("test"))
        with (
            mock.patch.object(system_module, "require_admin"),
            mock.patch.object(image_module, "config", self.config),
            mock.patch.object(image_module, "image_storage_service", self.storage),
            mock.patch.object(storage_module, "S3ImageClient") as client,
            TestClient(app) as http,
        ):
            client.return_value.delete.side_effect = ImageStorageError("R2/S3 DELETE failed: HTTP 403")
            response = http.post("/api/images/delete", json={"paths": [rel]})
        self.assertEqual(response.status_code, 502)
        self.assertIn("403", response.json()["detail"]["error"])
        self.assertTrue((self.config.images_dir / rel).exists())
        self.assertIn(rel, self.storage._load_index())

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
        with mock.patch("test.legacy_image_task_fixture.config", self.config):
            for _ in range(2):
                service = ImageTaskService(tasks)
                result = service.list_tasks({"id": "owner", "role": "admin"}, ["old-task"])
                self.assertEqual(result["missing_ids"], [])
                self.assertEqual(result["items"][0]["status"], "success")

    def test_scheduler_cleans_with_current_policy(self):
        old = self.image("scheduled.png", 10)
        remote = self.remote_image("scheduled-remote.png", 240)
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
            mock.patch.object(storage_module, "S3ImageClient") as client,
        ):
            image_module._auto_cleanup_worker(stop)
        self.assertFalse(old.exists())
        client.return_value.delete.assert_called_once_with(remote)

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
