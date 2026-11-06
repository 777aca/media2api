from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from api.app import create_app


class RemovedDebugPageTests(unittest.TestCase):
    def test_removed_debug_routes_never_serve_old_assets_or_home(self):
        with TemporaryDirectory() as directory:
            stale_page = Path(directory) / "index.html"
            stale_page.write_text("old debug page", encoding="utf-8")
            with mock.patch("api.app.resolve_web_asset", return_value=stale_page):
                client = TestClient(create_app())
                self.addCleanup(client.close)
                for path in ("/debug", "/debug/", "/debug/old"):
                    for method in ("get", "head"):
                        with self.subTest(path=path, method=method):
                            response = getattr(client, method)(path)
                            self.assertEqual(response.status_code, 404)
                            self.assertNotIn("old debug page", response.text)

    def test_creative_pages_still_serve_exported_assets(self):
        with TemporaryDirectory() as directory:
            page = Path(directory) / "index.html"
            page.write_text("creative page", encoding="utf-8")
            with mock.patch("api.app.resolve_web_asset", return_value=page):
                client = TestClient(create_app())
                self.addCleanup(client.close)
                for path in ("/ppt/", "/psd/", "/chat/", "/image/", "/video/"):
                    with self.subTest(path=path):
                        response = client.get(path)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.text, "creative page")
