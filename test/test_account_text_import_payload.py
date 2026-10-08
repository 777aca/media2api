from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("MEDIA2API_AUTH_KEY", "account-import-test-auth")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_module
from services.account_service import AccountService
from services.config import config
from services.storage.json_storage import JSONStorageBackend


AUTH_HEADERS = {"Authorization": "Bearer account-import-test-auth"}
CARD_ACCOUNT = {
    "access_token": "card-access-token",
    "email": "import@example.test",
    "password": "test-password",
    "totp_secret": "JBSWY3DPEHPK3PXP",
    "source_type": "web",
}


class AccountTextImportPayloadTests(unittest.TestCase):
    """验证文本解析后的账号载荷；所有凭据均为虚构测试数据。"""

    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory(prefix="media2api-account-import-")
        self.addCleanup(temporary_directory.cleanup)
        self.temporary_path = Path(temporary_directory.name)
        self.storage = JSONStorageBackend(self.temporary_path / "accounts.json")

        # AccountService 的累计计数和日志不跟随 storage，需要另外隔离。
        cumulative_patcher = mock.patch.object(
            AccountService,
            "_get_cumulative_file",
            return_value=self.temporary_path / ".cumulative_total",
        )
        cumulative_patcher.start()
        self.addCleanup(cumulative_patcher.stop)
        log_patcher = mock.patch("services.account_service.log_service.add")
        log_patcher.start()
        self.addCleanup(log_patcher.stop)
        config_patcher = mock.patch.dict(config.data, {"auto_relogin_after_refresh": False})
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

        self.service = AccountService(self.storage)
        service_patcher = mock.patch.object(accounts_module, "account_service", self.service)
        service_patcher.start()
        self.addCleanup(service_patcher.stop)
        admin_patcher = mock.patch.object(
            accounts_module,
            "require_admin",
            return_value={"id": "test-admin", "role": "admin"},
        )
        self.require_admin = admin_patcher.start()
        self.addCleanup(admin_patcher.stop)

        # 刷新流程仍走真实 service，远程信息查询在网络边界被替换。
        fetch_patcher = mock.patch.object(
            self.service,
            "fetch_remote_info",
            side_effect=lambda token, *_args: self.service.get_account(token),
        )
        self.fetch_remote_info = fetch_patcher.start()
        self.addCleanup(fetch_patcher.stop)
        app = FastAPI()
        app.include_router(accounts_module.create_router())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def assert_card_metadata(self, account: object) -> None:
        self.assertIsInstance(account, dict)
        if not isinstance(account, dict):
            self.fail("账号没有持久化")
        for field, expected in CARD_ACCOUNT.items():
            self.assertEqual(account.get(field), expected, field)
        self.assertEqual(account.get("type"), "free")
        self.assertNotIn("refresh_token", account)
        self.assertNotIn("id_token", account)

    def test_structured_card_metadata_survives_storage_reload(self) -> None:
        result = self.service.add_account_items([dict(CARD_ACCOUNT)])

        self.assertEqual(result["added"], 1)
        self.assert_card_metadata(self.service.get_account(CARD_ACCOUNT["access_token"]))
        reloaded_service = AccountService(self.storage)
        self.assert_card_metadata(reloaded_service.get_account(CARD_ACCOUNT["access_token"]))
        self.assertEqual(reloaded_service.list_tokens(), [CARD_ACCOUNT["access_token"]])

    def test_duplicate_card_and_token_import_preserves_metadata(self) -> None:
        first = self.service.add_account_items(
            [dict(CARD_ACCOUNT), {"access_token": "  card-access-token  "}]
        )
        second = self.service.add_accounts([CARD_ACCOUNT["access_token"]])

        self.assertEqual((first["added"], first["skipped"]), (1, 0))
        self.assertEqual((second["added"], second["skipped"]), (0, 1))
        self.assertEqual(len(self.storage.load_accounts()), 1)
        self.assert_card_metadata(AccountService(self.storage).get_account(CARD_ACCOUNT["access_token"]))

    def test_api_structured_card_refreshes_only_access_token(self) -> None:
        response = self.client.post(
            "/api/accounts", headers=AUTH_HEADERS, json={"accounts": [CARD_ACCOUNT]}
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["added"], 1)
        self.assertEqual(response.json()["refreshed"], 1)
        self.require_admin.assert_called_once_with(AUTH_HEADERS["Authorization"])
        self.fetch_remote_info.assert_called_once_with(
            CARD_ACCOUNT["access_token"], "refresh_accounts", True
        )
        self.assert_card_metadata(AccountService(self.storage).get_account(CARD_ACCOUNT["access_token"]))

    def test_api_mixed_card_and_plain_tokens_deduplicates_and_keeps_web_source(self) -> None:
        payload = {
            "accounts": [CARD_ACCOUNT, dict(CARD_ACCOUNT)],
            "tokens": ["  card-access-token  ", "plain-access-token", "plain-access-token", " "],
        }

        response = self.client.post("/api/accounts", headers=AUTH_HEADERS, json=payload)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["added"], 2)
        self.assertEqual(response.json()["refreshed"], 2)
        self.assertEqual(self.fetch_remote_info.call_count, 2)
        self.fetch_remote_info.assert_has_calls(
            [
                mock.call("card-access-token", "refresh_accounts", True),
                mock.call("plain-access-token", "refresh_accounts", True),
            ],
            any_order=True,
        )
        reloaded = AccountService(self.storage)
        self.assert_card_metadata(reloaded.get_account("card-access-token"))
        plain_account = reloaded.get_account("plain-access-token")
        self.assertIsNotNone(plain_account)
        self.assertEqual(plain_account["source_type"], "web")

        repeated = self.client.post("/api/accounts", headers=AUTH_HEADERS, json=payload)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual((repeated.json()["added"], repeated.json()["skipped"]), (0, 2))
        self.assertEqual(len(self.storage.load_accounts()), 2)
        self.assert_card_metadata(AccountService(self.storage).get_account("card-access-token"))

    def test_api_plain_token_import_remains_compatible(self) -> None:
        with mock.patch.object(
            self.service, "add_account_items", wraps=self.service.add_account_items
        ) as add_items:
            response = self.client.post(
                "/api/accounts",
                headers=AUTH_HEADERS,
                json={"tokens": ["  plain-access-token  ", "plain-access-token", ""]},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["added"], response.json()["refreshed"]), (1, 1))
        add_items.assert_not_called()
        self.fetch_remote_info.assert_called_once_with("plain-access-token", "refresh_accounts", True)
        self.assertEqual(self.service.get_account("plain-access-token")["source_type"], "web")

    def test_api_missing_access_token_rejects_without_refresh(self) -> None:
        response = self.client.post(
            "/api/accounts",
            headers=AUTH_HEADERS,
            json={"accounts": [{"email": "import@example.test", "password": "test-password"}]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error"], "tokens is required")
        self.assertEqual(self.storage.load_accounts(), [])
        self.fetch_remote_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
