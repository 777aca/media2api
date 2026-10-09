from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_module
from services.account_service import AccountService
from services.config import config
from services.model_service import ModelRoute
from services.storage.database_storage import DatabaseStorageBackend
from services.storage.json_storage import JSONStorageBackend


class SchedulingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="media2api-scheduling-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        for patcher in (
            mock.patch.object(AccountService, "_get_cumulative_file", return_value=self.path / ".total"),
            mock.patch("services.account_service.log_service.add"),
            mock.patch.dict(config.data, {"image_account_concurrency": 1, "auto_remove_rate_limited_accounts": False}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.storage = JSONStorageBackend(self.path / "accounts.json")
        self.service = AccountService(self.storage)
        self.service.refresh_access_token = lambda token, **_kwargs: token
        self.service.fetch_remote_info = lambda token, *_args: self.service.get_account(token)

    def add(self, token: str, **settings: object) -> None:
        self.service.add_account_items([{"access_token": token, "status": "正常", "quota": 20, **settings}])

    def image(self, **filters: object) -> str:
        token = self.service.get_available_access_token(**filters)
        self.service.release_image_slot(token)
        return token


class AccountSchedulingTests(SchedulingTestCase):
    def test_legacy_defaults_are_persisted_without_losing_metadata(self) -> None:
        identity = str(uuid.uuid4())
        self.storage.save_accounts([{"access_token": "legacy", "pool_account_id": identity, "success": 8,
                                    "refresh_token": "synthetic-refresh", "email": "test@example.test"}])
        migrated = AccountService(self.storage).get_account("legacy")
        self.assertEqual((migrated["priority"], migrated["weight"]), (0, 1))
        self.assertEqual(migrated["pool_account_id"], identity)
        self.assertEqual(migrated["success"], 8)
        self.assertEqual(migrated["refresh_token"], "synthetic-refresh")
        stored = self.storage.load_accounts()[0]
        self.assertEqual((stored["priority"], stored["weight"]), (0, 1))

    def test_invalid_stored_values_fall_back_to_safe_defaults(self) -> None:
        for priority, weight in ((True, True), (-1, 0), (1001, 1001), ("9", "4"), (None, []), (1.5, 2.5)):
            with self.subTest(priority=priority, weight=weight):
                self.storage.save_accounts([{"access_token": "invalid", "pool_account_id": str(uuid.uuid4()),
                                            "priority": priority, "weight": weight}])
                service = AccountService(self.storage)
                account = service.get_account("invalid")
                self.assertEqual((account["priority"], account["weight"]), (0, 1))
                self.assertIs(type(self.storage.load_accounts()[0]["weight"]), int)

    def test_defaults_keep_even_rotation_for_images_and_text(self) -> None:
        self.add("a")
        self.add("b")
        self.add("c")
        self.assertEqual([self.image() for _ in range(6)], ["a", "b", "c"] * 2)
        self.assertEqual([self.service.get_text_access_token() for _ in range(6)], ["a", "b", "c"] * 2)

    def test_priority_precedes_weight_and_falls_back_when_disabled(self) -> None:
        self.add("fallback", priority=0, weight=1000)
        self.add("preferred", priority=10, weight=1)
        self.assertEqual({self.image() for _ in range(10)}, {"preferred"})
        self.assertEqual({self.service.get_text_access_token() for _ in range(10)}, {"preferred"})
        self.service.update_account("preferred", {"status": "禁用"}, quiet=True)
        self.assertEqual(self.image(), "fallback")
        self.assertEqual(self.service.get_text_access_token(), "fallback")

    def test_equal_priority_distributes_by_weight_in_each_channel(self) -> None:
        self.add("heavy", weight=3)
        self.add("light", weight=1)
        image_counts = Counter()
        text_counts = Counter()
        for _ in range(40):
            image_counts[self.image()] += 1
            text_counts[self.service.get_text_access_token()] += 1
        self.assertEqual(image_counts, {"heavy": 30, "light": 10})
        self.assertEqual(text_counts, image_counts)

    def test_concurrent_text_selection_keeps_weighted_distribution(self) -> None:
        self.add("heavy", weight=3)
        self.add("light", weight=1)
        with ThreadPoolExecutor(max_workers=8) as executor:
            counts = Counter(executor.map(lambda _: self.service.get_text_access_token(), range(80)))
        self.assertEqual(counts, {"heavy": 60, "light": 20})

    def test_image_slot_limit_allows_lower_priority_fallback(self) -> None:
        self.add("preferred", priority=10)
        self.add("fallback")
        first = self.service.get_available_access_token()
        second = self.service.get_available_access_token()
        self.assertEqual((first, second), ("preferred", "fallback"))
        self.assertEqual(next(item for item in self.service.list_accounts() if item["access_token"] == first)["image_inflight"], 1)
        self.service.release_image_slot(first)
        self.assertEqual(self.image(), "preferred")
        self.service.release_image_slot(second)

    def test_unavailable_and_mismatched_accounts_cannot_override_image_filters(self) -> None:
        self.add("empty", priority=100, quota=0)
        self.add("abnormal", priority=100, status="异常")
        self.add("limited", priority=100, status="限流")
        self.add("disabled", priority=100, status="禁用")
        self.add("other-plan", priority=100, type="Pro")
        self.add("other-source", priority=100, source_type="codex", type="Plus")
        self.add("eligible", type="Plus", source_type="web")
        self.assertEqual(self.image(plan_type="plus", source_type="web", plan_types={"Plus"}), "eligible")
        self.service.update_account("eligible", {"quota": 0}, quiet=True)
        with self.assertRaisesRegex(RuntimeError, "no available"):
            self.image(plan_type="plus", source_type="web")

    def test_failed_image_validation_tries_lower_priority_and_releases_slot(self) -> None:
        self.add("preferred", priority=10)
        self.add("fallback")

        def remote(token: str, *_args: object) -> dict:
            if token == "preferred":
                raise RuntimeError("synthetic upstream failure")
            return self.service.get_account(token)

        self.service.fetch_remote_info = remote
        self.assertEqual(self.image(), "fallback")
        self.assertEqual(next(item for item in self.service.list_accounts() if item["access_token"] == "preferred")["image_inflight"], 0)

    def test_priority_respects_text_model_permissions_and_retry_exclusions(self) -> None:
        self.add("unauthorized", priority=100)
        self.add("preferred", priority=10)
        self.add("fallback")
        identities = frozenset(self.service.get_account(token)["pool_account_id"] for token in ("preferred", "fallback"))
        route = ModelRoute(account_ids=identities, allow_anonymous=False, codex_account_ids=identities)
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route):
            for channel in ("web", "codex"):
                self.assertEqual(self.service.get_text_access_token(model="test-model", channel=channel), "preferred")
                self.assertEqual(self.service.get_text_access_token(model="test-model", channel=channel,
                                                                   excluded_tokens={"preferred"}), "fallback")

    def test_changes_apply_immediately_and_survive_rotation_reimport_and_reload(self) -> None:
        self.add("a", weight=3)
        self.add("b")
        self.assertEqual(self.image(), "a")
        self.service._apply_refreshed_tokens("a", {"access_token": "new-a"}, "test")
        self.assertEqual(Counter(self.image() for _ in range(3)), {"new-a": 2, "b": 1})
        self.service.update_account("b", {"priority": 20, "weight": 7}, quiet=True)
        self.assertEqual(self.image(), "b")
        self.service.add_accounts(["b"])
        self.service.update_account("b", {"quota": 9}, quiet=True)
        account = AccountService(self.storage).get_account("b")
        self.assertEqual((account["priority"], account["weight"], account["quota"]), (20, 7, 9))
        self.service.delete_accounts(["b"])
        self.assertEqual(self.image(), "new-a")

    def test_database_storage_preserves_scheduling_fields(self) -> None:
        storage = DatabaseStorageBackend(f"sqlite:///{self.path / 'accounts.sqlite'}")
        self.addCleanup(storage.engine.dispose)
        service = AccountService(storage)
        service.add_account_items([{"access_token": "database-account", "priority": 5, "weight": 8}])
        service.update_account("database-account", {"priority": 9}, quiet=True)
        account = AccountService(storage).get_account("database-account")
        self.assertEqual((account["priority"], account["weight"]), (9, 8))


class AccountSchedulingAPITests(SchedulingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add("api-account", priority=4, weight=2)
        for patcher in (
            mock.patch.object(accounts_module, "account_service", self.service),
            mock.patch.object(accounts_module, "require_admin", return_value={"role": "admin"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(accounts_module.create_router())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_update_and_list_expose_saved_fields(self) -> None:
        response = self.client.post("/api/accounts/update", json={"access_token": "api-account", "priority": 0, "weight": 1000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["item"]["priority"], response.json()["item"]["weight"]), (0, 1000))
        listed = self.client.get("/api/accounts").json()["items"][0]
        self.assertEqual((listed["priority"], listed["weight"]), (0, 1000))
        self.assertEqual(AccountService(self.storage).get_account("api-account")["weight"], 1000)

    def test_partial_update_preserves_existing_scheduling(self) -> None:
        response = self.client.post("/api/accounts/update", json={"access_token": "api-account", "proxy": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["item"]["priority"], response.json()["item"]["weight"]), (4, 2))

    def test_invalid_updates_do_not_modify_account(self) -> None:
        for field, value in (("priority", -1), ("priority", 1001), ("weight", 0), ("weight", 1001),
                             ("weight", True), ("priority", False), ("weight", 1.5), ("weight", "3")):
            with self.subTest(field=field, value=value):
                response = self.client.post("/api/accounts/update", json={"access_token": "api-account", field: value})
                self.assertEqual(response.status_code, 422)
                account = self.service.get_account("api-account")
                self.assertEqual((account["priority"], account["weight"]), (4, 2))


if __name__ == "__main__":
    unittest.main()
