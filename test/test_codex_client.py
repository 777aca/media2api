from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

from services.codex_client import (
    DEFAULT_CODEX_CLIENT_VERSION, CodexClientVersion, _fetch_official_version,
    _stable_version, _version_from_release_url, codex_headers, get_codex_client_version,
)


class CodexClientTests(unittest.TestCase):
    def test_version_query_uses_upstream_proxy_without_account_credentials_and_falls_back(self) -> None:
        latest = mock.Mock(status_code=200)
        latest.json.return_value = {"tag_name": "rusty-v8-99.0.0", "draft": False, "prerelease": False}
        releases = mock.Mock(status_code=200)
        releases.json.return_value = [{"tag_name": "rust-v0.162.0", "draft": False, "prerelease": False}]
        with mock.patch("services.codex_client.requests.Session") as factory, \
                mock.patch("services.proxy_service.proxy_settings.build_session_kwargs", return_value={"proxy": "http://proxy.test:8080"}) as kwargs:
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [latest, releases]
            self.assertEqual(_fetch_official_version(), "0.162.0")
            kwargs.assert_called_once_with(upstream=True, impersonate="chrome")
            factory.assert_called_once_with(proxy="http://proxy.test:8080")
            self.assertEqual(session.get.call_count, 2)
            session.head.assert_not_called()
            for call in session.get.call_args_list:
                self.assertNotIn("Authorization", call.kwargs["headers"])
                self.assertNotIn("ChatGPT-Account-Id", call.kwargs["headers"])

    def test_github_api_forbidden_uses_canonical_latest_redirect_without_account_credentials(self) -> None:
        forbidden = mock.Mock(status_code=403)
        release = mock.Mock(status_code=200, url="https://github.com/openai/codex/releases/tag/rust-v0.163.0")
        with mock.patch("services.codex_client.requests.Session") as factory, \
                mock.patch("services.proxy_service.proxy_settings.build_session_kwargs", return_value={}) as kwargs:
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [forbidden, forbidden]
            session.head.return_value = release
            self.assertEqual(_fetch_official_version(), "0.163.0")
            kwargs.assert_called_once_with(upstream=True, impersonate="chrome")
            session.head.assert_called_once_with(
                "https://github.com/openai/codex/releases/latest",
                headers={"Accept": "text/html", "User-Agent": "media2api"},
                timeout=8, allow_redirects=True,
            )
            for call in [*session.get.call_args_list, *session.head.call_args_list]:
                self.assertNotIn("Authorization", call.kwargs["headers"])
                self.assertNotIn("ChatGPT-Account-Id", call.kwargs["headers"])

    def test_github_api_invalid_response_uses_canonical_latest_redirect(self) -> None:
        invalid = mock.Mock(status_code=200)
        invalid.json.side_effect = ValueError("synthetic invalid JSON")
        release = mock.Mock(status_code=200, url="https://github.com/openai/codex/releases/tag/rust-v0.162.0")
        with mock.patch("services.codex_client.requests.Session") as factory, \
                mock.patch("services.proxy_service.proxy_settings.build_session_kwargs", return_value={}):
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [invalid, invalid]
            session.head.return_value = release
            self.assertEqual(_fetch_official_version(), "0.162.0")

    def test_canonical_redirect_accepts_only_official_codex_stable_release_url(self) -> None:
        self.assertEqual(_version_from_release_url(
            "https://github.com/openai/codex/releases/tag/rust-v0.161.0"), "0.161.0")
        for url in (
            "http://github.com/openai/codex/releases/tag/rust-v0.163.0",
            "https://github.com.evil.test/openai/codex/releases/tag/rust-v0.163.0",
            "https://github.com@evil.test/openai/codex/releases/tag/rust-v0.163.0",
            "https://user:password@github.com/openai/codex/releases/tag/rust-v0.163.0",
            "https://github.com/other/codex/releases/tag/rust-v0.163.0",
            "https://github.com/openai/other/releases/tag/rust-v0.163.0",
            "https://github.com/openai/codex/releases/tag/rusty-v8-99.0.0",
            "https://github.com/openai/codex/releases/tag/rust-v0.163.0-alpha.1",
            "https://github.com/openai/codex/releases/tag/rust-v0.163.0/extra",
            "https://github.com/openai/codex/releases/tag/rust-v0.163.0?source=other",
            "https://github.com/openai/codex/releases/tag/rust-v0.163.0#fragment",
            "https://github.com/openai/codex/releases/tag/rust-v0.0163.0",
            "https://github.com/openai/codex/releases/tag/rust-v0.163.0\n",
            None,
        ):
            with self.subTest(url=url):
                self.assertIsNone(_version_from_release_url(url))

    def test_api_failure_and_untrusted_redirect_do_not_set_version(self) -> None:
        with mock.patch("services.codex_client.requests.Session") as factory, \
                mock.patch("services.proxy_service.proxy_settings.build_session_kwargs", return_value={}):
            session = factory.return_value.__enter__.return_value
            session.get.return_value = mock.Mock(status_code=403)
            session.head.return_value = mock.Mock(
                status_code=200, url="https://evil.test/openai/codex/releases/tag/rust-v9.0.0",
            )
            self.assertIsNone(_fetch_official_version())

    def test_official_stable_release_filters_tags_and_prereleases(self) -> None:
        self.assertEqual(_stable_version([
            {"tag_name": "rust-v0.162.0", "draft": False, "prerelease": False},
            {"tag_name": "rust-v0.200.0-alpha.1", "draft": False, "prerelease": True},
            {"tag_name": "rusty-v8-99.0.0", "draft": False, "prerelease": False},
            {"tag_name": "rust-v0.199.0", "draft": True, "prerelease": False},
            {"tag_name": "rust-v0.163.0", "draft": False, "prerelease": False},
        ]), "0.163.0")
        self.assertIsNone(_stable_version({"tag_name": "rust-vbad"}))

    def test_version_cache_updates_after_six_hours_without_downgrading(self) -> None:
        now = [0.0]
        fetch = mock.Mock(side_effect=["0.163.0", "0.162.0", RuntimeError("unavailable")])
        versions = CodexClientVersion(fetch=fetch, clock=lambda: now[0])
        self.assertEqual(versions.get(), "0.163.0")
        now[0] = 21599
        self.assertEqual(versions.get(), "0.163.0")
        self.assertEqual(fetch.call_count, 1)
        now[0] = 21601
        self.assertEqual(versions.get(), "0.163.0")
        now[0] += 21601
        self.assertEqual(versions.get(), "0.163.0")

    def test_first_failure_keeps_fallback_and_retries_after_five_minutes(self) -> None:
        now = [0.0]
        fetch = mock.Mock(side_effect=[None, "0.164.0"])
        versions = CodexClientVersion(fetch=fetch, clock=lambda: now[0])
        self.assertEqual(versions.get(), DEFAULT_CODEX_CLIENT_VERSION)
        now[0] = 301
        self.assertEqual(versions.get(), "0.164.0")

    def test_headers_share_version_and_use_real_account_id_from_token(self) -> None:
        payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "official-account"}}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"synthetic.{encoded}.signature"
        with mock.patch.dict("os.environ", {"MEDIA2API_CODEX_CLIENT_VERSION": "0.155.0"}):
            headers = codex_headers(token, {"pool_account_id": "internal-uuid"}, accept="text/event-stream")
        self.assertEqual(headers["ChatGPT-Account-Id"], "official-account")
        self.assertEqual(headers["Version"], "0.155.0")
        self.assertEqual(headers["User-Agent"], "codex_cli_rs/0.155.0")
        self.assertEqual(headers["Authorization"], f"Bearer {token}")
        self.assertEqual(headers["Accept"], "text/event-stream")

    def test_invalid_jwt_or_header_injection_does_not_add_account_header(self) -> None:
        with mock.patch.dict("os.environ", {"MEDIA2API_CODEX_CLIENT_VERSION": "0.146.0"}):
            headers = codex_headers("synthetic-token", {"chatgpt_account_id": "bad\r\ninjected"})
        self.assertNotIn("ChatGPT-Account-Id", headers)

    def test_invalid_or_too_old_override_uses_synchronized_version(self) -> None:
        for pinned in ("bad", "0.100.0", "0.146.0-alpha"):
            with mock.patch.dict("os.environ", {"MEDIA2API_CODEX_CLIENT_VERSION": pinned}), \
                    mock.patch("services.codex_client._client_version.get", return_value="0.156.0"):
                self.assertEqual(get_codex_client_version(), "0.156.0")
