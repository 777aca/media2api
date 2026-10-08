from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.account_service import AccountService
from services.model_service import model_catalog_service
from services.protocol import openai_v1_models
from services.storage.json_storage import JSONStorageBackend
from test.test_model_catalog_service import model_list


class ModelCatalogProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="media2api-protocol-models-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(AccountService, "_get_cumulative_file", return_value=self.path / ".total").start()
        mock.patch("services.account_service.log_service.add").start()
        self.accounts = AccountService(JSONStorageBackend(self.path / "accounts.json"))
        mock.patch.object(openai_v1_models, "account_service", self.accounts).start()
        self.official = model_list("official-new-model")
        self.catalog_call = mock.patch.object(model_catalog_service, "get_catalog", return_value={
            "object": "list", "data": [{**item, "source": "official"} for item in self.official["data"]],
        }).start()
        self.list_call = mock.patch.object(model_catalog_service, "list_models", return_value=self.official).start()
        self.addCleanup(self.catalog_call.assert_not_called)
        self.addCleanup(self.list_call.assert_not_called)

    def test_admin_catalog_only_contains_supported_image_entries_without_text_sync(self) -> None:
        self.accounts.add_account_items([
            {"access_token": "web-plus", "source_type": "web", "type": "Plus"},
            {"access_token": "codex-team", "source_type": "codex", "type": "Team"},
        ])
        result = openai_v1_models.get_catalog(force_refresh=True)
        by_id = {item["id"]: item for item in result["data"]}
        self.assertNotIn("official-new-model", by_id)
        self.assertEqual(by_id["gpt-image-2"]["source"], "compatibility")
        self.assertEqual(by_id["codex-gpt-image-2"]["source"], "compatibility")
        self.assertEqual(by_id["team-codex-gpt-image-2"]["source"], "compatibility")
        for model in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst"):
            self.assertEqual(by_id[model]["source"], "compatibility")
            self.assertEqual(by_id[model]["owned_by"], "media2api")
            self.assertEqual(by_id[model]["entry_kind"], "web_image")
        self.assertNotIn("gpt-image-2.5", by_id)
        self.assertNotIn("plus-codex-gpt-image-2", by_id)
        self.assertEqual(result["sync"]["status"], "success")
        self.assertEqual(result["sync"]["source_count"], 2)
        self.assertEqual(result["sync"]["successful_sources"], 2)
        self.assertEqual(result["sync"]["errors"], [])
        self.assertIsNotNone(result["sync"]["last_success_at"])

    def test_disabled_and_abnormal_accounts_do_not_supply_image_aliases(self) -> None:
        self.accounts.add_account_items([
            {"access_token": "web-disabled", "source_type": "web", "type": "Plus", "status": "禁用"},
            {"access_token": "codex-abnormal", "source_type": "codex", "type": "Team", "status": "异常"},
        ])
        result = openai_v1_models.get_catalog()
        self.assertEqual(result["data"], [])
        self.assertEqual(result["sync"]["source_count"], 0)

    def test_removed_account_immediately_removes_compatibility_alias(self) -> None:
        self.accounts.add_accounts(["web-account"])
        self.assertIn("gpt-image-2", {item["id"] for item in openai_v1_models.get_catalog()["data"]})
        self.accounts.delete_accounts(["web-account"])
        self.assertNotIn("gpt-image-2", {item["id"] for item in openai_v1_models.get_catalog()["data"]})

    def test_v1_response_has_only_openai_fields_without_sync_or_source(self) -> None:
        self.accounts.add_accounts(["web-account"])
        result = openai_v1_models.list_models(force_refresh=True)
        self.assertEqual(set(result), {"object", "data"})
        self.assertEqual(result["object"], "list")
        self.assertTrue(all("source" not in item for item in result["data"]))
        self.assertTrue(all("entry_kind" not in item for item in result["data"]))
        self.assertEqual(len({item["id"] for item in result["data"]}), len(result["data"]))

    def test_old_official_cache_does_not_contribute_to_the_image_catalog(self) -> None:
        self.accounts.add_accounts(["web-account"])
        self.catalog_call.return_value["data"].append({**model_list("gpt-image-2")["data"][0], "source": "official"})
        result = openai_v1_models.get_catalog()
        matches = [item for item in result["data"] if item["id"] == "gpt-image-2"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source"], "compatibility")
        self.assertNotIn("official-new-model", {item["id"] for item in result["data"]})

    def test_active_web_account_supplies_both_explicit_web_image_entries(self) -> None:
        self.accounts.add_account_items([{"access_token": "web-free", "type": "free", "source_type": "web"}])
        result = openai_v1_models.get_catalog()
        entries = {item["id"]: item for item in result["data"] if item.get("entry_kind") == "web_image"}
        self.assertEqual(set(entries), {"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"})
        self.assertTrue(all(item["source"] == "compatibility" and item["owned_by"] == "media2api"
                            for item in entries.values()))

    def test_disabling_or_deleting_last_account_removes_web_image_entries(self) -> None:
        for operation in ("禁用", "异常", "delete"):
            with self.subTest(operation=operation):
                self.accounts.add_account_items([{"access_token": "web-account", "status": "正常"}])
                self.assertTrue(any(item.get("entry_kind") == "web_image" for item in openai_v1_models.get_catalog()["data"]))
                if operation == "delete":
                    self.accounts.delete_accounts(["web-account"])
                else:
                    self.accounts.update_account("web-account", {"status": operation}, quiet=True)
                self.assertFalse(any(item.get("entry_kind") == "web_image" for item in openai_v1_models.get_catalog()["data"]))

    def test_blank_account_token_does_not_supply_web_image_entries(self) -> None:
        with mock.patch.object(self.accounts, "list_accounts", return_value=[{"access_token": "", "status": "正常"}]):
            result = openai_v1_models.get_catalog()
        self.assertFalse(any(item.get("entry_kind") == "web_image" for item in result["data"]))

    def test_multiple_accounts_are_deduplicated_and_reads_immediately_reflect_status_changes(self) -> None:
        self.accounts.add_accounts(["first-account", "second-account"])
        expected = {"gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}
        self.assertEqual({item["id"] for item in openai_v1_models.list_models()["data"]}, expected)
        self.accounts.update_account("first-account", {"status": "禁用"}, quiet=True)
        result = openai_v1_models.get_catalog(force_refresh=True)
        self.assertEqual(len(result["data"]), 3)
        self.assertEqual(result["sync"]["source_count"], 1)
        self.accounts.delete_accounts(["second-account"])
        self.assertEqual(openai_v1_models.list_models()["data"], [])

    def test_public_and_admin_catalogs_have_identical_image_ids(self) -> None:
        self.accounts.add_account_items([
            {"access_token": "codex-plus", "source_type": "codex", "type": "Plus"},
            {"access_token": "codex-pro", "source_type": "codex", "type": "Pro"},
        ])
        public = openai_v1_models.list_models()
        admin = openai_v1_models.get_catalog(force_refresh=True)
        self.assertEqual([item["id"] for item in public["data"]], [item["id"] for item in admin["data"]])
        self.assertTrue(all("image" in item["id"] for item in public["data"]))
        self.assertEqual(admin["sync"]["source_count"], 2)


if __name__ == "__main__":
    unittest.main()
