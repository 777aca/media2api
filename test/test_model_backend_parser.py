from __future__ import annotations

import unittest
from unittest import mock

from services.openai_backend_api import OpenAIBackendAPI
from services.proxy_service import ProxySettingsStore
from test.test_proxy_service import FakeConfig, make_runtime


class ModelBackendParserTests(unittest.TestCase):
    def backend(self, payload: object) -> OpenAIBackendAPI:
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "synthetic-token"
        backend.base_url = "https://example.test"
        backend.account = {"pool_account_id": "synthetic-account-id"}
        backend.session = mock.Mock()
        backend.session.get.return_value.json.return_value = payload
        backend.session.get.return_value.status_code = 200
        backend._bootstrap = mock.Mock()
        backend._headers = mock.Mock(return_value={})
        backend.close = mock.Mock()
        return backend

    def test_official_slugs_keep_exact_case_and_characters_and_deduplicate(self) -> None:
        backend = self.backend({"models": [{"slug": "GPT-New_2026-preview"}, {"slug": "gpt-new"},
                                           {"slug": "GPT-New_2026-preview"}]})
        result = backend.list_models()
        self.assertEqual([item["id"] for item in result["data"]], ["GPT-New_2026-preview", "gpt-new"])
        self.assertEqual(result["object"], "list")
        self.assertTrue(all(item["owned_by"] == "chatgpt" for item in result["data"]))

    def test_missing_or_invalid_models_shape_and_rows_fail_instead_of_empty_success(self) -> None:
        for payload in ({}, {"models": None}, [], {"models": {}}, {"models": [None]},
                        {"models": [{"name": "model-without-slug"}]}, {"models": [{"slug": 10}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(Exception) as caught:
                    self.backend(payload).list_models()
                self.assertEqual(type(caught.exception).__name__, "InvalidModelResponseError")

    def test_valid_empty_models_is_successful_empty_catalog(self) -> None:
        self.assertEqual(self.backend({"models": []}).list_models(), {"object": "list", "data": []})

    def test_codex_accepts_models_data_and_bare_array_envelopes(self) -> None:
        entries = [{"slug": "gpt.official-latest", "display_name": "Official latest"}]
        for payload in ({"models": entries}, {"object": "list", "data": entries}, entries):
            with self.subTest(payload=payload):
                result = OpenAIBackendAPI._normalize_model_manifest(payload)
                self.assertEqual([item["id"] for item in result["data"]], ["gpt.official-latest"])
                self.assertEqual(result["data"][0]["display_name"], "Official latest")
                self.assertEqual(result["object"], "list")

    def test_codex_identifier_priority_deduplication_and_exact_values(self) -> None:
        result = OpenAIBackendAPI._normalize_model_manifest({"models": [
            {"id": "GPT.Exact_2026-preview", "slug": "ignored-slug", "name": "ignored-name"},
            {"slug": "slug-ID", "name": "ignored-name"},
            {"name": "name-only-ID"},
            {"id": "GPT.Exact_2026-preview"},
            {"id": "", "slug": "fallback-slug"},
        ]})
        self.assertEqual([item["id"] for item in result["data"]],
                         ["GPT.Exact_2026-preview", "fallback-slug", "name-only-ID", "slug-ID"])
        self.assertTrue(all(item["root"] == item["id"] for item in result["data"]))

    def test_codex_merges_models_and_data_when_both_are_returned(self) -> None:
        for models in ([{"slug": "manifest-model"}, {"id": "shared-model"}], []):
            with self.subTest(models=models):
                result = OpenAIBackendAPI._normalize_model_manifest({
                    "models": models,
                    "data": [{"id": "api-model"}, {"id": "shared-model"}],
                })
                expected = {"api-model", "shared-model"} | {item.get("id") or item.get("slug") for item in models}
                self.assertEqual({item["id"] for item in result["data"]}, expected)
                self.assertEqual(len(result["data"]), len(expected))

    def test_codex_invalid_secondary_envelope_fails_instead_of_discarding_it(self) -> None:
        for data in (None, {}, [None], [{"id": 17}]):
            with self.subTest(data=data):
                with self.assertRaises(Exception) as caught:
                    OpenAIBackendAPI._normalize_model_manifest({"models": [{"slug": "good-model"}], "data": data})
                self.assertEqual(type(caught.exception).__name__, "InvalidModelResponseError")

    def test_codex_hidden_and_unsupported_flags_do_not_remove_official_returned_models(self) -> None:
        result = OpenAIBackendAPI._normalize_model_manifest({"models": [
            {"slug": "visible-model", "visibility": "list", "supported_in_api": True},
            {"slug": "hidden-model", "visibility": "hide", "supported_in_api": False},
            {"slug": "unknown-visibility-model", "visibility": "hidden", "hidden": True},
        ]})
        self.assertEqual([item["id"] for item in result["data"]],
                         ["hidden-model", "unknown-visibility-model", "visible-model"])

    def test_codex_manifest_omits_internal_prompts_credentials_and_configuration(self) -> None:
        result = OpenAIBackendAPI._normalize_model_manifest({"models": [{
            "id": "public-model", "display_name": "Public model", "created": "123",
            "owned_by": "official-owner", "instructions": "private upstream instructions",
            "base_instructions": "private prompt", "access_token": "private-token",
            "model_messages": {"private": "private configuration"},
        }]})
        self.assertEqual(result["data"][0], {
            "id": "public-model", "object": "model", "created": 123,
            "owned_by": "official-owner", "permission": [], "root": "public-model", "parent": None,
            "display_name": "Public model",
        })

    def test_codex_malformed_envelopes_and_rows_fail_instead_of_empty_success(self) -> None:
        for payload in ({}, {"models": None}, {"data": None}, {"models": {}},
                        {"models": [None]}, {"models": [{}]}, {"models": [{"id": 42, "slug": "valid"}]},
                        {"models": [{"slug": " whitespace "}]}, {"models": [{"id": "model", "created": {}}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(Exception) as caught:
                    OpenAIBackendAPI._normalize_model_manifest(payload)
                self.assertEqual(type(caught.exception).__name__, "InvalidModelResponseError")
                self.assertEqual(str(caught.exception), "invalid_response")

    def test_codex_valid_empty_manifest_is_successful_empty_catalog(self) -> None:
        for payload in ({"models": []}, {"data": []}, []):
            with self.subTest(payload=payload):
                self.assertEqual(OpenAIBackendAPI._normalize_model_manifest(payload), {"object": "list", "data": []})

    def test_codex_requests_official_endpoint_with_consistent_client_version_without_web_bootstrap(self) -> None:
        backend = self.backend({"models": [{"slug": "official-codex"}]})
        headers = {"Version": "0.146.0", "Authorization": "Bearer synthetic-token"}
        with mock.patch("services.openai_backend_api.codex_headers", return_value=headers) as build_headers:
            result = backend.list_codex_models()
        self.assertEqual(result["data"][0]["id"], "official-codex")
        build_headers.assert_called_once_with("synthetic-token", account=backend.account)
        backend.session.get.assert_called_once_with(
            "https://example.test/backend-api/codex/models", headers=headers,
            params={"client_version": "0.146.0"}, timeout=30,
        )
        backend._bootstrap.assert_not_called()

    def test_codex_invalid_json_error_is_redacted(self) -> None:
        backend = self.backend(None)
        backend.session.get.return_value.json.side_effect = ValueError("private token in invalid response")
        with mock.patch("services.openai_backend_api.codex_headers", return_value={"Version": "0.146.0"}):
            with self.assertRaises(Exception) as caught:
                backend.list_codex_models()
        self.assertEqual(type(caught.exception).__name__, "InvalidModelResponseError")
        self.assertEqual(str(caught.exception), "invalid_response")

    def test_codex_anonymous_access_rejected_before_network_request(self) -> None:
        backend = self.backend({"models": []})
        backend.access_token = ""
        with self.assertRaises(RuntimeError):
            backend.list_codex_models()
        backend.session.get.assert_not_called()

    def test_backend_constructor_honors_enabled_runtime_proxy_for_upstream_sessions(self) -> None:
        settings = ProxySettingsStore(FakeConfig(legacy_proxy="http://legacy.example:8080", runtime=make_runtime(
            enabled=True, egress_mode="single_proxy", proxy_url="http://runtime.example:8080",
        )))
        session = mock.Mock()
        session.headers = {}
        with mock.patch("services.openai_backend_api.proxy_settings", settings), \
                mock.patch("services.openai_backend_api.account_service.get_account", return_value={}), \
                mock.patch("services.openai_backend_api.requests.Session", return_value=session) as create_session:
            backend = OpenAIBackendAPI("synthetic-token")
        self.addCleanup(backend.close)
        self.assertEqual(create_session.call_args.kwargs["proxy"], "http://runtime.example:8080")
        self.assertEqual(create_session.call_args.kwargs["verify"], True)
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_backend_constructor_prioritizes_account_proxy_over_runtime_proxy(self) -> None:
        settings = ProxySettingsStore(FakeConfig(legacy_proxy="http://legacy.example:8080", runtime=make_runtime(
            enabled=True, egress_mode="single_proxy", proxy_url="http://runtime.example:8080",
        )))
        session = mock.Mock()
        session.headers = {}
        account = {"proxy": "socks://account.example:1080"}
        with mock.patch("services.openai_backend_api.proxy_settings", settings), \
                mock.patch("services.openai_backend_api.account_service.get_account", return_value=account), \
                mock.patch("services.openai_backend_api.requests.Session", return_value=session) as create_session:
            backend = OpenAIBackendAPI("synthetic-token")
        self.addCleanup(backend.close)
        self.assertEqual(create_session.call_args.kwargs["proxy"], "socks5h://account.example:1080")
        session.get.assert_not_called()
        session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
