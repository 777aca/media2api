from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

os.environ.setdefault("MEDIA2API_AUTH_KEY", "account-dedup-test-auth")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_module
from services.account_service import AccountService
from services.config import config
from services.providers.grok_accounts import parse_import
from services.providers.grok_store import GrokStore
from services.storage.json_storage import JSONStorageBackend


AUTH_HEADERS = {"Authorization": "Bearer account-dedup-test-auth"}


def make_jwt(payload: dict[str, Any]) -> str:
    def encode(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f'{encode({"alg": "none", "typ": "JWT"})}.{encode(payload)}.sig'


class AccountDeduplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="media2api-dedup-tests-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.storage = JSONStorageBackend(self.path / "accounts.json")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(AccountService, "_get_cumulative_file", return_value=self.path / ".total").start()
        mock.patch("services.account_service.log_service.add").start()
        mock.patch.dict(config.data, {"auto_relogin_after_refresh": False}).start()
        self.service = AccountService(self.storage)

    def test_reimport_same_email_with_new_token_rotates_and_skips_duplicate(self) -> None:
        first = self.service.add_account_items([
            {
                "access_token": "old-token",
                "email": "User@Example.Test",
                "password": "synthetic-password",
                "priority": 12,
                "weight": 5,
                "source_type": "codex",
            }
        ])
        identity = self.service.get_account("old-token")["pool_account_id"]

        second = self.service.add_account_items([
            {
                "access_token": "new-token",
                "email": "user@example.test",
                "source_type": "web",
            }
        ])

        self.assertEqual((first["added"], first["skipped"]), (1, 0))
        self.assertEqual((second["added"], second["skipped"]), (0, 1))
        self.assertEqual(len(self.storage.load_accounts()), 1)
        account = self.service.get_account("new-token")
        self.assertIsNotNone(account)
        self.assertEqual(account["pool_account_id"], identity)
        self.assertEqual(account["password"], "synthetic-password")
        self.assertEqual(account["priority"], 12)
        self.assertEqual(account["weight"], 5)
        self.assertEqual(account["source_type"], "codex")
        self.assertEqual(self.service.resolve_access_token("old-token"), "new-token")

    def test_plain_jwt_tokens_deduplicate_by_jwt_identity_and_keep_newer_exp(self) -> None:
        older_token = make_jwt({
            "exp": 1_800_000_000,
            "sub": "auth0|user-1",
            "https://api.openai.com/auth": {"chatgpt_user_id": "user-aaa", "chatgpt_account_id": "acct-1"},
            "https://api.openai.com/profile": {"email": "jwt@example.test"},
        })
        newer_token = make_jwt({
            "exp": 1_900_000_000,
            "sub": "auth0|user-1",
            "https://api.openai.com/auth": {"chatgpt_user_id": "user-aaa", "chatgpt_account_id": "acct-1"},
            "https://api.openai.com/profile": {"email": "jwt@example.test"},
        })

        first = self.service.add_accounts([newer_token], source_type="codex")
        second = self.service.add_accounts([older_token], source_type="web")

        self.assertEqual((first["added"], first["skipped"]), (1, 0))
        self.assertEqual((second["added"], second["skipped"]), (0, 1))
        self.assertEqual(len(self.storage.load_accounts()), 1)
        self.assertEqual(self.service.list_tokens(), [newer_token])
        self.assertEqual(self.service.resolve_access_token(older_token), newer_token)
        account = self.service.get_account(newer_token)
        self.assertEqual(account["email"], "jwt@example.test")
        self.assertEqual(account["user_id"], "user-aaa")
        self.assertEqual(account["source_type"], "codex")

    def test_same_batch_duplicate_identity_with_different_tokens_imports_once(self) -> None:
        older_token = make_jwt({
            "exp": 1_800_000_000,
            "https://api.openai.com/auth": {"chatgpt_user_id": "user-batch"},
            "https://api.openai.com/profile": {"email": "batch@example.test"},
        })
        newer_token = make_jwt({
            "exp": 1_900_000_000,
            "https://api.openai.com/auth": {"chatgpt_user_id": "user-batch"},
            "https://api.openai.com/profile": {"email": "batch@example.test"},
        })

        result = self.service.add_accounts([older_token, newer_token])

        self.assertEqual((result["added"], result["skipped"]), (1, 0))
        self.assertEqual(self.service.list_tokens(), [newer_token])
        self.assertEqual(self.service.resolve_access_token(older_token), newer_token)

    def test_team_members_sharing_workspace_account_id_are_not_treated_as_duplicates(self) -> None:
        member_a = make_jwt({
            "exp": 1_900_000_000,
            "sub": "auth0|member-a",
            "https://api.openai.com/auth": {"chatgpt_user_id": "user-a", "chatgpt_account_id": "team-workspace-1"},
            "https://api.openai.com/profile": {"email": "a@team.test"},
        })
        member_b = make_jwt({
            "exp": 1_900_000_000,
            "sub": "auth0|member-b",
            "https://api.openai.com/auth": {"chatgpt_user_id": "user-b", "chatgpt_account_id": "team-workspace-1"},
            "https://api.openai.com/profile": {"email": "b@team.test"},
        })

        result = self.service.add_accounts([member_a, member_b])

        self.assertEqual((result["added"], result["skipped"]), (2, 0))
        self.assertEqual(len(self.storage.load_accounts()), 2)

    def test_opaque_token_merges_with_existing_account_after_remote_refresh(self) -> None:
        self.service.add_account_items([
            {
                "access_token": "existing-opaque",
                "email": "opaque@example.test",
                "user_id": "user-opaque",
                "priority": 9,
                "weight": 4,
            }
        ])
        identity = self.service.get_account("existing-opaque")["pool_account_id"]

        # Opaque token carries no JWT claims, so identity is only discovered on update/refresh.
        self.service.add_accounts(["new-opaque"])
        self.assertEqual(len(self.storage.load_accounts()), 2)

        updated = self.service.update_account(
            "new-opaque",
            {"email": "opaque@example.test", "user_id": "user-opaque", "quota": 5, "status": "正常"},
            quiet=True,
        )

        self.assertIsNotNone(updated)
        self.assertEqual(len(self.storage.load_accounts()), 1)
        self.assertEqual(self.service.list_tokens(), ["new-opaque"])
        self.assertEqual(updated["pool_account_id"], identity)
        self.assertEqual((updated["priority"], updated["weight"], updated["quota"]), (9, 4, 5))
        self.assertEqual(self.service.resolve_access_token("existing-opaque"), "new-opaque")

    def test_oauth_finish_reimport_deduplicates_and_refreshes_once(self) -> None:
        mock.patch.object(accounts_module, "account_service", self.service).start()
        mock.patch.object(accounts_module, "require_admin", return_value={"id": "admin", "role": "admin"}).start()
        fetch_mock = mock.patch.object(
            self.service,
            "fetch_remote_info",
            side_effect=lambda token, *_args: self.service.get_account(token),
        ).start()

        app = FastAPI()
        app.include_router(accounts_module.create_router())
        client = TestClient(app)
        self.addCleanup(client.close)

        token_1 = make_jwt({
            "exp": 1_800_000_000,
            "https://api.openai.com/auth": {"chatgpt_user_id": "user-oauth"},
            "https://api.openai.com/profile": {"email": "oauth@example.test"},
        })
        token_2 = make_jwt({
            "exp": 1_900_000_000,
            "https://api.openai.com/auth": {"chatgpt_user_id": "user-oauth"},
            "https://api.openai.com/profile": {"email": "oauth@example.test"},
        })

        with mock.patch.object(
            accounts_module.oauth_login_service,
            "finish",
            side_effect=[
                {"access_token": token_1, "refresh_token": "rt-1", "id_token": "id-1"},
                {"access_token": token_2, "refresh_token": "rt-2", "id_token": "id-2"},
            ],
        ):
            first = client.post("/api/accounts/oauth/finish", headers=AUTH_HEADERS, json={"session_id": "s1", "callback": "c1"})
            second = client.post("/api/accounts/oauth/finish", headers=AUTH_HEADERS, json={"session_id": "s2", "callback": "c2"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual((first.json()["added"], first.json()["skipped"]), (1, 0))
        self.assertEqual(second.status_code, 200)
        self.assertEqual((second.json()["added"], second.json()["skipped"]), (0, 1))
        self.assertEqual(len(self.storage.load_accounts()), 1)
        self.assertEqual(self.service.list_tokens(), [token_2])
        self.assertEqual(fetch_mock.call_count, 2)


class GrokAccountDeduplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="media2api-grok-dedup-")
        self.addCleanup(directory.cleanup)
        self.store = GrokStore(Path(directory.name))
        self.addCleanup(self.store.db.close)

    def test_grok_import_deduplicates_by_email_and_user_id_when_sso_rotates(self) -> None:
        entries = parse_import([
            {"sso_token": "sso-1", "email": "Grok@Example.Test", "user_id": "grok-u1", "name": "First"},
            {"sso_token": "sso-2", "email": "grok@example.test", "name": "Updated"},
        ])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sso_token"], "sso-2")
        self.assertEqual(entries[0]["user_id"], "grok-u1")

        self.store.import_accounts(entries)
        original_id = self.store.accounts()[0]["id"]

        self.store.import_accounts(parse_import([
            {"sso_token": "sso-3", "user_id": "grok-u1", "tier": "super"},
        ]))

        accounts = self.store.accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["id"], original_id)
        self.assertEqual(accounts[0]["tier"], "super")
        self.assertEqual(self.store.account(original_id, secret=True)["sso_token"], "sso-3")


if __name__ == "__main__":
    unittest.main()
