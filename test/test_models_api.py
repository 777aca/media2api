from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.ai as ai_module
import api.support as support_module


class ModelsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = {
            "object": "list", "data": [{"id": "gpt-image-2.5-flare", "object": "model", "source": "compatibility", "entry_kind": "web_image"}],
            "sync": {"status": "success", "last_attempt_at": "2026-10-08T00:00:00+00:00",
                     "last_success_at": "2026-10-08T00:00:00+00:00", "source_count": 1,
                     "successful_sources": 1, "errors": []},
        }
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(support_module, "config", SimpleNamespace(auth_key="test-admin-key")).start()
        identities = {"test-user-key": {"id": "test-user", "name": "test", "role": "user"}}
        mock.patch.object(support_module.auth_service, "authenticate", side_effect=identities.get).start()
        self.get_catalog = mock.patch.object(ai_module.openai_v1_models, "get_catalog", return_value=self.catalog).start()
        self.list_models = mock.patch.object(ai_module.openai_v1_models, "list_models", return_value={
            "object": "list", "data": [{"id": "gpt-image-2.5-flare", "object": "model"}],
        }).start()
        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_admin_catalog_get_and_force_sync_forward_refresh_flag(self) -> None:
        headers = {"Authorization": "Bearer test-admin-key"}
        response = self.client.get("/api/models", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.catalog)
        self.get_catalog.assert_called_once_with(force_refresh=False)
        self.get_catalog.reset_mock()
        response = self.client.post("/api/models/sync", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.catalog)
        self.get_catalog.assert_called_once_with(force_refresh=True)

    def test_admin_catalog_endpoints_reject_missing_invalid_and_user_keys(self) -> None:
        for headers, expected in (({}, 401), ({"Authorization": "Bearer invalid-key"}, 401),
                                  ({"Authorization": "Bearer test-user-key"}, 403)):
            for method, path in ((self.client.get, "/api/models"), (self.client.post, "/api/models/sync")):
                with self.subTest(headers=headers, path=path):
                    response = method(path, headers=headers)
                    self.assertEqual(response.status_code, expected)
        self.get_catalog.assert_not_called()

    def test_v1_models_keeps_openai_shape_and_accepts_user_identity(self) -> None:
        response = self.client.get("/v1/models", headers={"Authorization": "Bearer test-user-key"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(set(result), {"object", "data"})
        self.assertEqual(result["object"], "list")
        self.assertNotIn("source", result["data"][0])
        self.list_models.assert_called_once_with()

    def test_unexpected_catalog_exception_is_redacted_at_http_boundary(self) -> None:
        self.get_catalog.side_effect = RuntimeError("private-access-token private-password")
        for method, path in ((self.client.get, "/api/models"), (self.client.post, "/api/models/sync")):
            with self.subTest(path=path):
                response = method(path, headers={"Authorization": "Bearer test-admin-key"})
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("private-", response.text)

    def test_public_catalog_exception_does_not_expose_account_data(self) -> None:
        self.list_models.side_effect = RuntimeError("private-access-token private-password")
        response = self.client.get("/v1/models", headers={"Authorization": "Bearer test-user-key"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private-", response.text)


if __name__ == "__main__":
    unittest.main()
