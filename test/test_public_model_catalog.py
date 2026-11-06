from __future__ import annotations

import unittest
from unittest import mock

from services.model_service import model_catalog_service
from services.protocol import openai_v1_models


class PublicModelCatalogTests(unittest.TestCase):
    def test_combines_text_and_images_without_duplicate_ids(self):
        images = {"object": "list", "data": [{"id": "gpt-image-2", "owned_by": "media2api"}]}
        official = {"object": "list", "data": [
            {"id": "gpt-text-test", "owned_by": "chatgpt"},
            {"id": "gpt-image-2", "owned_by": "chatgpt"},
        ]}
        with mock.patch.object(openai_v1_models, "list_models", return_value=images), \
                mock.patch.object(model_catalog_service, "list_models", return_value=official) as upstream:
            result = openai_v1_models.list_all_models(force_refresh=True)
        upstream.assert_called_once_with(force_refresh=True)
        self.assertEqual(result["object"], "list")
        self.assertEqual(result["data"], [images["data"][0], official["data"][0]])

    def test_unexpected_text_catalog_failure_keeps_image_entries(self):
        images = {"object": "list", "data": [{"id": "gpt-image-2"}]}
        with mock.patch.object(openai_v1_models, "list_models", return_value=images), \
                mock.patch.object(model_catalog_service, "list_models", side_effect=RuntimeError("private-token")):
            result = openai_v1_models.list_all_models()
        self.assertEqual(result, images)


class TextModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.official = {
            "object": "list", "data": [
                {"id": "gpt-text-test", "source": "official", "channels": ["web"]},
                {"id": "gpt-image-2", "source": "official", "channels": ["web"]},
            ],
            "sync": {"status": "success", "last_attempt_at": "2026-11-06T00:00:00Z",
                     "last_success_at": "2026-11-06T00:00:00Z", "source_count": 1,
                     "successful_sources": 1, "errors": []},
        }
        self.upstream = mock.patch.object(model_catalog_service, "get_catalog", return_value=self.official).start()
        self.store = mock.patch("services.providers.grok_store.get_store").start().return_value
        self.store.accounts.return_value = []

    def test_text_catalog_excludes_images_and_preserves_channel(self):
        result = openai_v1_models.get_text_catalog(force_refresh=True)
        self.upstream.assert_called_once_with(force_refresh=True)
        self.assertEqual([m["id"] for m in result["data"]], ["gpt-text-test"])
        self.assertEqual(result["data"][0]["channels"], ["web"])
        self.assertEqual(result["data"][0]["capabilities"], ["chat"])

    def test_grok_chat_survives_failed_official_sync_without_images_or_video(self):
        self.official["data"] = []
        self.official["sync"].update(status="failed", successful_sources=0, last_success_at=None,
            errors=[{"account_id": "synthetic", "source": "account", "channel": "web", "code": "upstream_http_401"}])
        self.store.accounts.return_value = [{"enabled": True, "tier": "basic"}, {"enabled": False, "tier": "heavy"}]
        result = openai_v1_models.get_text_catalog()
        self.assertEqual([m["id"] for m in result["data"]], ["grok-chat-fast"])
        self.assertEqual(result["sync"]["status"], "partial")
        self.assertEqual(result["sync"]["source_count"], 2)
        self.assertEqual(result["sync"]["successful_sources"], 1)
        self.assertEqual(result["sync"]["errors"][0]["code"], "upstream_http_401")

    def test_empty_pool_does_not_invent_models(self):
        self.official["data"] = []
        self.official["sync"].update(source_count=0, successful_sources=0)
        self.store.accounts.return_value = [{"enabled": False}, {"status": "reauth_required"}]
        result = openai_v1_models.get_text_catalog()
        self.assertEqual(result["data"], [])
        self.assertEqual(result["sync"]["source_count"], 0)


if __name__ == "__main__":
    unittest.main()
