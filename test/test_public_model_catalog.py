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


if __name__ == "__main__":
    unittest.main()
