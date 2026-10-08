from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from services.account_service import AccountService
from services.storage.json_storage import JSONStorageBackend


class AccountIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="media2api-identity-tests-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.storage = JSONStorageBackend(self.path / "accounts.json")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(AccountService, "_get_cumulative_file", return_value=self.path / ".total").start()
        mock.patch("services.account_service.log_service.add").start()

    def test_legacy_record_migration_preserves_credentials_metadata_and_persists_identity(self) -> None:
        legacy = {"access_token": "legacy-token", "refresh_token": "legacy-refresh", "email": "test@example.test",
                  "password": "fake-password", "totp_secret": "fake-totp", "account_id": "official-account-id",
                  "type": "Plus", "source_type": "web", "status": "正常", "success": 19, "fail": 2}
        self.storage.save_accounts([legacy])
        service = AccountService(self.storage)
        migrated = service.get_account("legacy-token")
        identity = migrated["pool_account_id"]
        self.assertEqual(uuid.UUID(identity).version, 4)
        for key, expected in legacy.items():
            self.assertEqual(migrated[key], expected, key)
        self.assertEqual(self.storage.load_accounts()[0]["pool_account_id"], identity)
        self.assertEqual(AccountService(self.storage).get_account("legacy-token")["pool_account_id"], identity)

    def test_duplicate_stored_identity_is_repaired_and_does_not_share_permissions(self) -> None:
        identity = str(uuid.uuid4())
        self.storage.save_accounts([
            {"access_token": "first-token", "pool_account_id": identity},
            {"access_token": "second-token", "pool_account_id": identity},
        ])
        service = AccountService(self.storage)
        first = service.get_account("first-token")["pool_account_id"]
        second = service.get_account("second-token")["pool_account_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(uuid.UUID(first).version, 4)
        self.assertEqual(uuid.UUID(second).version, 4)
        self.assertEqual({item["pool_account_id"] for item in self.storage.load_accounts()}, {first, second})

    def test_refresh_and_metadata_update_keep_identity_across_reload(self) -> None:
        service = AccountService(self.storage)
        service.add_account_items([{"access_token": "old-token", "refresh_token": "old-refresh", "type": "Plus"}])
        identity = service.get_account("old-token")["pool_account_id"]
        service._apply_refreshed_tokens("old-token", {"access_token": "new-token", "refresh_token": "new-refresh"}, "test")
        service.update_account("new-token", {"type": "Pro", "status": "禁用"}, quiet=True)
        reloaded = AccountService(self.storage).get_account("new-token")
        self.assertEqual(reloaded["pool_account_id"], identity)
        self.assertEqual(reloaded["refresh_token"], "new-refresh")
        self.assertEqual(reloaded["type"], "Pro")
        self.assertEqual(reloaded["status"], "禁用")

    def test_import_and_updates_cannot_assign_another_accounts_internal_identity(self) -> None:
        service = AccountService(self.storage)
        service.add_accounts(["first-token"])
        identity = service.get_account("first-token")["pool_account_id"]
        service.add_account_items([{"access_token": "second-token", "pool_account_id": identity}])
        second_identity = service.get_account("second-token")["pool_account_id"]
        self.assertNotEqual(second_identity, identity)
        service.update_account("second-token", {"pool_account_id": identity}, quiet=True)
        self.assertEqual(service.get_account("second-token")["pool_account_id"], second_identity)
        service.add_account_items([{"access_token": "first-token", "pool_account_id": str(uuid.uuid4()), "email": "new@example.test"}])
        self.assertEqual(service.get_account("first-token")["pool_account_id"], identity)


if __name__ == "__main__":
    unittest.main()
