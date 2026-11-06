from __future__ import annotations

import tempfile
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from PIL import Image

from services.config import _normalize_image_storage_settings, _validate_image_storage_settings
from services.image_storage_service import ImageStorageService, S3ImageClient


def png_bytes() -> bytes:
    path = Path(tempfile.gettempdir()) / "media2api-test-image.png"
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(path, format="PNG")
    return path.read_bytes()


class FakeWebDAVClient:
    uploaded: dict[str, bytes] = {}
    deleted: list[str] = []

    def __init__(self, _settings):
        pass

    def put(self, rel: str, payload: bytes) -> str:
        self.uploaded[rel] = payload
        return f"https://dav.example.test/{rel}"

    def get(self, rel: str) -> bytes:
        return self.uploaded[rel]

    def delete(self, rel: str) -> bool:
        self.deleted.append(rel)
        self.uploaded.pop(rel, None)
        return True

    def test(self) -> dict[str, object]:
        self.put(".media2api_webdav_test.txt", b"media2api webdav test\n")
        self.delete(".media2api_webdav_test.txt")
        return {"ok": True, "status": 200, "error": None}


class FakeS3Client:
    uploaded: dict[str, bytes] = {}
    deleted: list[str] = []

    def __init__(self, _settings):
        pass

    def put(self, rel: str, payload: bytes) -> str:
        self.uploaded[rel] = payload
        return f"https://r2.example.test/bucket/media2api/images/{rel}"

    def get(self, rel: str) -> bytes:
        return self.uploaded[rel]

    def delete(self, rel: str) -> bool:
        self.deleted.append(rel)
        self.uploaded.pop(rel, None)
        return True

    def test(self) -> dict[str, object]:
        self.put(".media2api_s3_test.txt", b"media2api s3 test\n")
        self.delete(".media2api_s3_test.txt")
        return {"ok": True, "status": 200, "error": None}


class ImageStorageServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.images_dir = self.data_dir / "images"
        self.settings = {
            "enabled": False,
            "provider": "webdav",
            "mode": "local",
            "webdav_url": "",
            "webdav_username": "",
            "webdav_password": "",
            "webdav_root_path": "media2api/images",
            "s3_account_id": "",
            "s3_endpoint": "",
            "s3_region": "auto",
            "s3_access_key_id": "",
            "s3_secret_access_key": "",
            "s3_bucket": "",
            "s3_prefix": "media2api/images",
            "public_base_url": "",
        }
        self.config_patcher = mock.patch("services.image_storage_service.config")
        self.mock_config = self.config_patcher.start()
        self.addCleanup(self.config_patcher.stop)
        self.mock_config.images_dir = self.images_dir
        self.mock_config.base_url = "http://app.test"
        self.mock_config.cleanup_old_images.return_value = 0
        self.mock_config.get_image_storage_settings.side_effect = lambda: dict(self.settings)
        FakeWebDAVClient.uploaded = {}
        FakeWebDAVClient.deleted = []
        FakeS3Client.uploaded = {}
        FakeS3Client.deleted = []

    def service(self) -> ImageStorageService:
        return ImageStorageService(self.data_dir / "image_index.json")

    def test_new_image_time_is_stored_with_utc8_offset(self):
        instant = datetime.fromisoformat("2026-11-05T16:00:01+00:00").timestamp()
        service = self.service()
        with mock.patch("services.image_storage_service.time.time", return_value=instant):
            stored = service.save(png_bytes())
        index = json.loads((self.data_dir / "image_index.json").read_text())
        self.assertEqual(index["items"][stored.rel]["created_at"], "2026-11-06 00:00:01+08:00")
        self.assertEqual(index["items"][stored.rel]["date"], "2026-11-06")

    def test_legacy_remote_images_filter_and_sort_by_utc8_not_directory_date(self):
        service = self.service()
        before = int(datetime.fromisoformat("2026-11-05T15:59:59Z").timestamp())
        after = before + 1
        paths = [f"2026/11/05/{instant}_abc.png" for instant in (before, after)]
        index = {rel: {"date": "2026-11-05", "created_at": stamp, "s3": True,
                       "local": False, "storage": "s3"}
                 for rel, stamp in zip(paths, ["2026-11-05 15:59:59", "2026-11-05 16:00:00"])}
        index_path = self.data_dir / "image_index.json"
        index_path.write_text(json.dumps({"items": index}))
        original = index_path.read_bytes()
        items = service.list_items("http://app.test", "2026-11-06", "2026-11-06")
        self.assertEqual([item["rel"] for item in items], [paths[1]])
        self.assertEqual(items[0]["created_at"], "2026-11-06 00:00:00")
        self.assertEqual(items[0]["date"], "2026-11-06")
        self.assertEqual([item["rel"] for item in service.list_items("http://app.test")], paths[::-1])
        self.assertEqual(index_path.read_bytes(), original)

    def test_explicit_offset_is_converted_and_unindexed_file_uses_mtime(self):
        service = self.service()
        index = {"remote.png": {"date": "2026-11-05", "created_at": "2026-11-05T17:00:00Z",
                                "s3": True, "local": False, "storage": "s3"}}
        (self.data_dir / "image_index.json").write_text(json.dumps({"items": index}))
        self.images_dir.mkdir()
        path = self.images_dir / "imported.png"
        path.write_bytes(png_bytes())
        instant = datetime.fromisoformat("2026-11-05T16:00:00Z").timestamp()
        os.utime(path, (instant, instant))
        items = service.list_items("http://app.test", "2026-11-06", "2026-11-06")
        self.assertEqual([item["created_at"] for item in items],
                         ["2026-11-06 01:00:00", "2026-11-06 00:00:00"])

    def test_local_mode_saves_to_local_directory(self):
        stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(stored.storage, "local")
        self.assertTrue((self.images_dir / stored.rel).is_file())
        self.assertEqual(stored.url, f"http://app.test/images/{stored.rel}")

    def test_webdav_mode_uploads_without_local_file(self):
        self.settings.update({
            "enabled": True,
            "mode": "webdav",
            "webdav_url": "https://dav.example.test",
            "webdav_password": "secret",
        })
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            stored = self.service().save(png_bytes(), "http://app.test")
            payload = self.service().get_bytes(stored.rel)

        self.assertEqual(stored.storage, "webdav")
        self.assertFalse((self.images_dir / stored.rel).exists())
        self.assertIn(stored.rel, FakeWebDAVClient.uploaded)
        self.assertEqual(payload, FakeWebDAVClient.uploaded[stored.rel])

    def test_s3_mode_uploads_without_local_file_and_returns_public_r2_url(self):
        self.settings.update({
            "enabled": True,
            "provider": "s3",
            "mode": "s3",
            "s3_account_id": "acc123",
            "s3_access_key_id": "ak123",
            "s3_secret_access_key": "sk123",
            "s3_bucket": "images-bucket",
            "s3_prefix": "media2api/images",
            "public_base_url": "https://pub-xxx.r2.dev",
        })
        with mock.patch("services.image_storage_service.S3ImageClient", FakeS3Client):
            stored = self.service().save(png_bytes(), "http://app.test")
            payload = self.service().get_bytes(stored.rel)

        self.assertEqual(stored.storage, "s3")
        self.assertFalse((self.images_dir / stored.rel).exists())
        self.assertIn(stored.rel, FakeS3Client.uploaded)
        self.assertEqual(payload, FakeS3Client.uploaded[stored.rel])
        self.assertEqual(stored.url, f"https://pub-xxx.r2.dev/media2api/images/{stored.rel}")

    def test_s3_both_mode_and_sync_all(self):
        image = png_bytes()
        image_path = self.images_dir / "2026" / "11" / "02" / "sample.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(image)

        self.settings.update({
            "enabled": True,
            "provider": "s3",
            "mode": "both",
            "s3_account_id": "acc123",
            "s3_access_key_id": "ak123",
            "s3_secret_access_key": "sk123",
            "s3_bucket": "images-bucket",
            "s3_prefix": "media2api/images",
            "public_base_url": "https://pub-xxx.r2.dev/media2api/images",
        })
        with mock.patch("services.image_storage_service.S3ImageClient", FakeS3Client):
            sync_result = self.service().sync_all()
            test_result = self.service().test_webdav()
            items = self.service().list_items("http://app.test")

        self.assertEqual(sync_result, {"uploaded": 1, "skipped": 0, "failed": 0})
        self.assertTrue(test_result["ok"])
        self.assertIn(".media2api_s3_test.txt", FakeS3Client.deleted)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["storage"], "both")
        self.assertEqual(items[0]["url"], "https://pub-xxx.r2.dev/media2api/images/2026/11/02/sample.png")

    def test_s3_client_normalizes_cloudflare_r2_endpoint_and_signs_request(self):
        client = S3ImageClient({
            "s3_endpoint": "https://acc123.r2.cloudflarestorage.com/my-bucket",
            "s3_bucket": "my-bucket",
            "s3_region": "auto",
            "s3_access_key_id": "AKID",
            "s3_secret_access_key": "SECRET",
            "s3_prefix": "media2api/images",
        })
        self.assertEqual(client.endpoint, "https://acc123.r2.cloudflarestorage.com")
        self.assertEqual(client.object_key("2026/11/02/a.png"), "media2api/images/2026/11/02/a.png")
        _, headers = client._aws_v4_headers("PUT", "/my-bucket/media2api/images/2026/11/02/a.png", body=b"abc")
        self.assertIn("AWS4-HMAC-SHA256 Credential=AKID/", headers["authorization"])

    def test_config_validation_for_s3_and_webdav(self):
        normalized = _normalize_image_storage_settings({
            "enabled": True,
            "provider": "r2",
            "mode": "webdav",
            "s3_account_id": "acc123",
            "s3_access_key_id": "ak123",
            "s3_secret_access_key": "sk123",
            "s3_bucket": "images",
        })
        self.assertEqual(normalized["provider"], "s3")
        self.assertEqual(normalized["mode"], "s3")
        _validate_image_storage_settings(normalized)

        with self.assertRaisesRegex(ValueError, "Bucket"):
            _validate_image_storage_settings({**normalized, "s3_bucket": ""})

    def test_list_items_ignores_non_image_files(self):
        image = png_bytes()
        image_path = self.images_dir / "2026" / "05" / "07" / "sample.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(image)
        (self.images_dir / ".DS_Store").write_text("not an image", encoding="utf-8")
        (self.images_dir / "2026" / ".DS_Store").write_text("not an image", encoding="utf-8")

        items = self.service().list_items("http://app.test")

        self.assertEqual([item["rel"] for item in items], ["2026/05/07/sample.png"])
        self.assertEqual(items[0]["storage"], "local")

    def test_both_mode_saves_to_local_and_webdav(self):
        self.settings.update({
            "enabled": True,
            "mode": "both",
            "webdav_url": "https://dav.example.test",
            "webdav_password": "secret",
            "public_base_url": "https://cdn.example.test/images",
        })
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(stored.storage, "both")
        self.assertTrue((self.images_dir / stored.rel).is_file())
        self.assertIn(stored.rel, FakeWebDAVClient.uploaded)
        self.assertEqual(stored.url, f"https://cdn.example.test/images/{stored.rel}")

    def test_test_webdav_writes_and_deletes_probe_file(self):
        self.settings.update({
            "enabled": True,
            "mode": "webdav",
            "webdav_url": "https://dav.example.test",
            "webdav_password": "secret",
        })
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            result = self.service().test_webdav()

        self.assertTrue(result["ok"])
        self.assertIn(".media2api_webdav_test.txt", FakeWebDAVClient.deleted)


if __name__ == "__main__":
    unittest.main()
