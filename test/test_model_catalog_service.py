from __future__ import annotations

import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from unittest import mock

from services.account_service import AccountService
from services.model_service import ModelCatalogService
from services.storage.json_storage import JSONStorageBackend


def model_list(*model_ids: str) -> dict:
    return {"object": "list", "data": [
        {"id": model_id, "object": "model", "created": 0, "owned_by": "chatgpt",
         "permission": [], "root": model_id, "parent": None}
        for model_id in model_ids
    ]}


class FakeBackend:
    def __init__(self, token: str, owner: ModelCatalogServiceTests) -> None:
        self.token = token
        self.owner = owner
        self.channel = "web"

    def list_models(self) -> object:
        return self._fetch("web", self.owner.outcomes)

    def list_codex_models(self) -> object:
        return self._fetch("codex", self.owner.codex_outcomes)

    def _fetch(self, channel: str, outcomes: dict[str, object]) -> object:
        self.channel = channel
        with self.owner.activity_lock:
            self.owner.calls.append((channel, self.token))
            self.owner.active_fetches += 1
            self.owner.maximum_fetches = max(self.owner.maximum_fetches, self.owner.active_fetches)
            if self.owner.active_fetches == 4:
                self.owner.four_sources_started.set()
        try:
            if not self.owner.release_fetches.wait(timeout=5):
                raise TimeoutError("test fetch gate timed out")
            outcome = outcomes.get(self.token, model_list())
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            with self.owner.activity_lock:
                self.owner.active_fetches -= 1

    def close(self) -> None:
        with self.owner.activity_lock:
            self.owner.closed.append((self.channel, self.token))


class ModelCatalogServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory(prefix="media2api-model-tests-")
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(AccountService, "_get_cumulative_file", return_value=self.root / ".total").start()
        mock.patch("services.account_service.log_service.add").start()
        self.accounts = AccountService(JSONStorageBackend(self.root / "accounts.json"))
        self.accounts.add_account_items([
            {"access_token": "free-a", "type": "free", "status": "正常"},
            {"access_token": "free-b", "type": "FREE", "status": "正常"},
            {"access_token": "plus", "type": "Plus", "status": "正常"},
            {"access_token": "pro", "type": "pro", "status": "正常"},
            {"access_token": "team-disabled", "type": "Team", "status": "禁用"},
            {"access_token": "team-abnormal", "type": "Team", "status": "异常"},
        ])
        self.accounts.refresh_access_token = lambda token, **_kwargs: token
        self.now = 1000.0
        self.calls: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.active_fetches = 0
        self.maximum_fetches = 0
        self.activity_lock = Lock()
        self.four_sources_started = Event()
        self.release_fetches = Event()
        self.release_fetches.set()
        self.outcomes: dict[str, object] = {
            "": model_list("anon", "shared"),
            "free-a": model_list("free-a-only", "shared"),
            "free-b": model_list("free-b-only", "shared"),
            "plus": model_list("plus-only", "shared"),
            "pro": model_list("pro-only"),
        }
        # Existing web-only fixtures have no advertised Codex models. Tests that
        # cover Codex access set independent outcomes below.
        self.codex_outcomes: dict[str, object] = {
            token: model_list() for token in self.outcomes if token
        }
        self.catalog = ModelCatalogService(
            self.accounts,
            backend_factory=lambda access_token="": FakeBackend(access_token, self),
            cache_ttl_seconds=300, clock=lambda: self.now,
        )

    def account_id(self, token: str) -> str:
        return self.accounts.get_account(token)["pool_account_id"]

    def test_catalog_unions_every_active_account_without_plan_shortcut(self) -> None:
        result = self.catalog.list_models()
        self.assertEqual([item["id"] for item in result["data"]],
                         ["anon", "free-a-only", "free-b-only", "plus-only", "pro-only", "shared"])
        self.assertCountEqual(self.calls, [("web", token) for token in self.outcomes]
                              + [("codex", token) for token in self.outcomes if token])
        self.assertCountEqual(self.closed, self.calls)
        route = self.catalog.route_for_model("free-b-only")
        self.assertEqual(route.account_ids, frozenset({self.account_id("free-b")}))
        self.assertFalse(route.allow_anonymous)
        shared = self.catalog.route_for_model("shared")
        self.assertEqual(shared.account_ids, frozenset(self.account_id(t) for t in ("free-a", "free-b", "plus")))
        self.assertTrue(shared.allow_anonymous)

    def test_ttl_cache_and_force_refresh(self) -> None:
        self.catalog.list_models()
        self.now += 299
        self.catalog.list_models()
        self.catalog.route_for_model("pro-only")
        self.assertEqual(self.calls.count(("web", "pro")), 1)
        self.assertEqual(self.calls.count(("codex", "pro")), 1)
        self.catalog.list_models(force_refresh=True)
        self.assertEqual(self.calls.count(("web", "pro")), 2)
        self.assertEqual(self.calls.count(("codex", "pro")), 2)
        self.now += 301
        self.catalog.list_models()
        self.assertEqual(self.calls.count(("web", "pro")), 3)
        self.assertEqual(self.calls.count(("codex", "pro")), 3)

    def test_overlapping_force_refreshes_share_one_batch_and_four_source_limit(self) -> None:
        for index in range(5):
            token = f"extra-{index}"
            self.accounts.add_accounts([token])
            self.outcomes[token] = model_list(f"extra-model-{index}")
        self.release_fetches.clear()
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                first = executor.submit(self.catalog.list_models, force_refresh=True)
                self.assertTrue(self.four_sources_started.wait(timeout=3))
                others = [executor.submit(self.catalog.list_models, force_refresh=True) for _ in range(7)]
                time.sleep(0.1)
                self.release_fetches.set()
                results = [first.result(timeout=5), *[future.result(timeout=5) for future in others]]
        finally:
            self.release_fetches.set()
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.calls), 19)
        self.assertLessEqual(self.maximum_fetches, 4)
        self.assertEqual(len(set(self.calls)), len(self.calls))

    def test_failed_account_refresh_preserves_its_previous_models(self) -> None:
        self.catalog.list_models()
        self.outcomes["pro"] = RuntimeError("temporary upstream failure")
        self.now += 301
        result = self.catalog.list_models()
        self.assertIn("pro-only", {item["id"] for item in result["data"]})
        self.assertEqual(self.catalog.route_for_model("pro-only").account_ids, frozenset({self.account_id("pro")}))
        self.assertEqual(self.catalog.get_catalog()["sync"]["status"], "partial")

    def test_deleted_disabled_and_abnormal_accounts_drop_cached_permissions(self) -> None:
        self.codex_outcomes["pro"] = model_list("pro-codex-only")
        for operation in ("delete", "禁用", "异常"):
            with self.subTest(operation=operation):
                self.catalog.list_models()
                if operation == "delete":
                    self.accounts.delete_accounts(["pro"])
                else:
                    self.accounts.update_account("pro", {"status": operation}, quiet=True)
                result = self.catalog.list_models()
                self.assertNotIn("pro-only", {item["id"] for item in result["data"]})
                self.assertNotIn("pro-codex-only", {item["id"] for item in result["data"]})
                self.assertEqual(self.catalog.route_for_model("pro-only").account_ids, frozenset())
                self.assertEqual(self.catalog.route_for_model("pro-codex-only").account_ids, frozenset())
                self.accounts.add_account_items([{"access_token": "pro", "type": "Pro", "status": "正常"}])
                self.accounts.update_account("pro", {"status": "正常"}, quiet=True)

    def test_equal_size_account_replacement_invalidates_catalog(self) -> None:
        self.catalog.list_models()
        previous_id = self.account_id("pro")
        self.accounts.delete_accounts(["pro"])
        self.accounts.add_account_items([{"access_token": "pro-new", "type": "Pro"}])
        self.outcomes["pro-new"] = model_list("new-model")
        result = self.catalog.list_models()
        self.assertNotIn("pro-only", {item["id"] for item in result["data"]})
        self.assertIn("new-model", {item["id"] for item in result["data"]})
        self.assertNotEqual(self.account_id("pro-new"), previous_id)

    def test_accounts_removed_or_disabled_during_sync_cannot_reappear_from_inflight_result(self) -> None:
        self.release_fetches.clear()
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(self.catalog.list_models)
                self.assertTrue(self.four_sources_started.wait(timeout=3))
                self.accounts.delete_accounts(["free-a"])
                self.accounts.update_account("free-b", {"status": "禁用"}, quiet=True)
                self.release_fetches.set()
                result = pending.result(timeout=5)
        finally:
            self.release_fetches.set()
        self.assertNotIn("free-a-only", {item["id"] for item in result["data"]})
        self.assertNotIn("free-b-only", {item["id"] for item in result["data"]})
        self.assertEqual(self.catalog.route_for_model("free-a-only").account_ids, frozenset())
        self.assertEqual(self.catalog.route_for_model("free-b-only").account_ids, frozenset())

    def test_token_rotation_keeps_permission_owner_and_fetches_rotated_token(self) -> None:
        self.catalog.list_models()
        previous_id = self.account_id("pro")
        self.accounts._apply_refreshed_tokens("pro", {"access_token": "pro-rotated"}, "test")
        self.outcomes["pro-rotated"] = RuntimeError("temporary upstream failure")
        result = self.catalog.list_models()
        self.assertIn("pro-only", {item["id"] for item in result["data"]})
        self.assertEqual(self.catalog.route_for_model("pro-only").account_ids, frozenset({previous_id}))
        self.assertIn(("web", "pro-rotated"), self.calls)
        self.assertIn(("codex", "pro-rotated"), self.calls)
        reloaded = AccountService(self.accounts.storage)
        self.assertEqual(reloaded.get_account("pro-rotated")["pool_account_id"], previous_id)

    def test_invalid_upstream_shape_does_not_erase_good_cache(self) -> None:
        self.catalog.list_models()
        for malformed in ({"unexpected": []}, {"data": None}, []):
            with self.subTest(malformed=malformed):
                self.outcomes["pro"] = malformed
                result = self.catalog.get_catalog(force_refresh=True)
                self.assertIn("pro-only", {item["id"] for item in result["data"]})
                self.assertEqual(result["sync"]["status"], "partial")
                self.assertTrue(result["sync"]["errors"])

    def test_sync_metadata_errors_and_logs_never_include_credentials(self) -> None:
        initial = self.catalog.get_catalog()
        self.assertEqual(initial["sync"]["status"], "success")
        self.assertEqual(initial["sync"]["source_count"], 9)
        self.assertEqual(initial["sync"]["successful_sources"], 9)
        self.assertIsNotNone(initial["sync"]["last_success_at"])
        self.assertEqual(initial["sync"]["errors"], [])
        for token in list(self.outcomes):
            self.outcomes[token] = RuntimeError("secret-access-token secret-password secret-totp")
        for token in self.codex_outcomes:
            self.codex_outcomes[token] = RuntimeError("secret-access-token secret-password secret-totp")
        with mock.patch("services.model_service.logger.warning") as logged:
            result = self.catalog.get_catalog(force_refresh=True)
        self.assertEqual(result["sync"]["status"], "failed")
        self.assertEqual(result["sync"]["successful_sources"], 0)
        self.assertEqual(result["sync"]["last_success_at"], initial["sync"]["last_success_at"])
        self.assertEqual({item["id"] for item in result["data"]}, {item["id"] for item in initial["data"]})
        for output in (json.dumps(result), str(logged.call_args_list)):
            self.assertNotIn("secret-", output)
        for secret in ("free-a", "free-b", "plus", "pro"):
            self.assertNotIn(secret, json.dumps(result["sync"]))
        for error in result["sync"]["errors"]:
            self.assertIn(error["source"], {"account", "anonymous"})
            self.assertIn(error["channel"], {"web", "codex"})
            self.assertTrue(error["code"])

    def test_first_failure_has_no_success_time_or_fabricated_models(self) -> None:
        for token in list(self.outcomes):
            self.outcomes[token] = RuntimeError("private credential")
        for token in self.codex_outcomes:
            self.codex_outcomes[token] = RuntimeError("private credential")
        result = self.catalog.get_catalog()
        self.assertEqual(result["data"], [])
        self.assertEqual(result["sync"]["status"], "failed")
        self.assertIsNone(result["sync"]["last_success_at"])
        self.assertIsNotNone(result["sync"]["last_attempt_at"])

    def test_empty_successful_catalog_removes_old_models(self) -> None:
        self.catalog.list_models()
        self.outcomes["pro"] = model_list()
        result = self.catalog.list_models(force_refresh=True)
        self.assertNotIn("pro-only", {item["id"] for item in result["data"]})
        self.assertEqual(self.catalog.route_for_model("pro-only").account_ids, frozenset())

    def test_same_model_across_channels_deduplicates_without_merging_permissions(self) -> None:
        self.codex_outcomes["free-b"] = model_list("free-a-only", "gpt.official-codex")
        result = self.catalog.get_catalog()
        matches = [item for item in result["data"] if item["id"] == "free-a-only"]
        self.assertEqual(len(matches), 1)
        self.assertCountEqual(matches[0]["channels"], ["web", "codex"])
        route = self.catalog.route_for_model("free-a-only")
        self.assertEqual(route.account_ids, frozenset({self.account_id("free-a"), self.account_id("free-b")}))
        self.assertEqual(route.account_ids_for_channel("web"), frozenset({self.account_id("free-a")}))
        self.assertEqual(route.account_ids_for_channel("codex"), frozenset({self.account_id("free-b")}))
        codex_route = self.catalog.route_for_model("gpt.official-codex")
        self.assertEqual(codex_route.account_ids_for_channel("web"), frozenset())
        self.assertEqual(codex_route.account_ids_for_channel("codex"), frozenset({self.account_id("free-b")}))
        self.assertFalse(codex_route.allow_anonymous)
        public = self.catalog.list_models()
        self.assertTrue(all("channels" not in item and "source" not in item for item in public["data"]))

    def test_web_account_attempts_codex_models_without_source_type_shortcut(self) -> None:
        self.accounts.update_account("free-a", {"source_type": "web"}, quiet=True)
        self.codex_outcomes["free-a"] = model_list("official-codex-model")
        result = self.catalog.get_catalog()
        model = next(item for item in result["data"] if item["id"] == "official-codex-model")
        self.assertEqual(model["channels"], ["codex"])
        self.assertIn(("codex", "free-a"), self.calls)

    def test_each_account_refreshes_token_once_for_both_channels(self) -> None:
        with mock.patch.object(self.accounts, "refresh_access_token", side_effect=lambda token, **_kwargs: token) as refresh:
            self.catalog.get_catalog()
        self.assertEqual(refresh.call_count, 4)
        self.assertCountEqual([call.args[0] for call in refresh.call_args_list], ["free-a", "free-b", "plus", "pro"])
        self.assertTrue(all(call.kwargs == {"event": "model_catalog_sync"} for call in refresh.call_args_list))

    def test_model_map_does_not_copy_internal_upstream_fields_into_responses(self) -> None:
        self.codex_outcomes["pro"] = {"data": [{
            **model_list("official-codex-model")["data"][0],
            "display_name": "Public model", "instructions": "private-prompt",
            "access_token": "private-token", "nested": {"password": "private-password"},
        }]}
        result = self.catalog.get_catalog()
        model = next(item for item in result["data"] if item["id"] == "official-codex-model")
        self.assertEqual(model["display_name"], "Public model")
        self.assertNotIn("private-", json.dumps(result))
        self.assertNotIn("instructions", model)
        self.assertNotIn("access_token", model)

    def test_channel_failure_preserves_only_that_channel_cache_while_other_updates(self) -> None:
        self.codex_outcomes["pro"] = model_list("codex-previous")
        initial = self.catalog.get_catalog()
        self.outcomes["pro"] = model_list("web-replacement")
        self.codex_outcomes["pro"] = RuntimeError("private token inside error")
        result = self.catalog.get_catalog(force_refresh=True)
        ids = {item["id"] for item in result["data"]}
        self.assertNotIn("pro-only", ids)
        self.assertIn("web-replacement", ids)
        self.assertIn("codex-previous", ids)
        self.assertEqual(result["sync"]["status"], "partial")
        self.assertEqual(result["sync"]["successful_sources"], 8)
        self.assertEqual(result["sync"]["last_success_at"], initial["sync"]["last_success_at"])
        self.assertEqual(len(result["sync"]["errors"]), 1)
        self.assertEqual(result["sync"]["errors"][0]["channel"], "codex")
        self.assertEqual(result["sync"]["errors"][0]["account_id"], self.account_id("pro"))
        self.assertEqual(self.catalog.route_for_model("codex-previous").account_ids_for_channel("web"), frozenset())

        self.outcomes["pro"] = RuntimeError("web unavailable")
        self.codex_outcomes["pro"] = model_list("codex-replacement")
        result = self.catalog.get_catalog(force_refresh=True)
        ids = {item["id"] for item in result["data"]}
        self.assertIn("web-replacement", ids)
        self.assertNotIn("codex-previous", ids)
        self.assertIn("codex-replacement", ids)
        self.assertEqual(result["sync"]["errors"][0]["channel"], "web")

    def test_codex_permission_survives_token_rotation_and_transient_failure(self) -> None:
        self.codex_outcomes["pro"] = model_list("official-codex-model")
        self.catalog.list_models()
        previous_id = self.account_id("pro")
        self.accounts._apply_refreshed_tokens("pro", {"access_token": "pro-rotated"}, "test")
        self.outcomes["pro-rotated"] = model_list("new-web-model")
        self.codex_outcomes["pro-rotated"] = RuntimeError("transient unavailable")
        result = self.catalog.get_catalog()
        self.assertIn("official-codex-model", {item["id"] for item in result["data"]})
        route = self.catalog.route_for_model("official-codex-model")
        self.assertEqual(route.account_ids_for_channel("codex"), frozenset({previous_id}))
        self.assertEqual(route.account_ids_for_channel("web"), frozenset())

    def test_invalid_codex_response_preserves_prior_codex_and_current_web_models(self) -> None:
        self.codex_outcomes["pro"] = model_list("official-codex-model")
        self.catalog.list_models()
        self.outcomes["pro"] = model_list("updated-web-model")
        for malformed in ({"unexpected": []}, {"data": None}, []):
            with self.subTest(malformed=malformed):
                self.codex_outcomes["pro"] = malformed
                result = self.catalog.get_catalog(force_refresh=True)
                self.assertIn("official-codex-model", {item["id"] for item in result["data"]})
                self.assertIn("updated-web-model", {item["id"] for item in result["data"]})
                self.assertEqual(result["sync"]["status"], "partial")
                self.assertEqual(result["sync"]["errors"][0]["channel"], "codex")

    def test_codex_failure_without_permission_keeps_real_web_catalog(self) -> None:
        # An account whose Codex endpoint rejects access contributes only its
        # successful web catalog; no Codex IDs are synthesized as a fallback.
        for token in self.codex_outcomes:
            self.codex_outcomes[token] = RuntimeError("upstream rejected Codex access")
        result = self.catalog.get_catalog()
        self.assertEqual(result["sync"]["status"], "partial")
        self.assertEqual(result["sync"]["successful_sources"], 5)
        self.assertTrue(all(item["channels"] == ["web"] for item in result["data"]))
        self.assertEqual(len(result["sync"]["errors"]), 4)
        self.assertTrue(all(error["channel"] == "codex" for error in result["sync"]["errors"]))

    def test_disabled_during_sync_cannot_reintroduce_codex_permissions(self) -> None:
        self.codex_outcomes["free-a"] = model_list("codex-in-flight")
        self.release_fetches.clear()
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(self.catalog.get_catalog)
                self.assertTrue(self.four_sources_started.wait(timeout=3))
                self.accounts.update_account("free-a", {"status": "禁用"}, quiet=True)
                self.release_fetches.set()
                result = pending.result(timeout=5)
        finally:
            self.release_fetches.set()
        self.assertNotIn("codex-in-flight", {item["id"] for item in result["data"]})
        self.assertEqual(self.catalog.route_for_model("codex-in-flight").account_ids_for_channel("codex"), frozenset())


if __name__ == "__main__":
    unittest.main()
