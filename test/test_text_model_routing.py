from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.account_service import AccountService
from services.model_service import ModelRoute, ModelUnavailableError
from services.protocol import (
    anthropic_v1_messages,
    conversation,
    openai_v1_chat_complete,
    openai_v1_response,
)
from services.storage.json_storage import JSONStorageBackend


class TextAccountRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.service = AccountService(
            JSONStorageBackend(Path(self.temp_dir.name) / "accounts.json")
        )
        self.service.add_account_items(
            [
                {"access_token": "free", "type": "free", "status": "正常"},
                {"access_token": "plus", "type": "Plus", "status": "正常"},
                {"access_token": "pro", "type": "Pro", "status": "正常"},
                {"access_token": "pro-other", "type": "Pro", "status": "正常"},
                {"access_token": "pro-disabled", "type": "Pro", "status": "禁用"},
            ]
        )
        self.service.refresh_access_token = lambda token, **_kwargs: token

    def test_explicit_model_selects_only_advertising_account_even_with_same_plan(self) -> None:
        account_id = self.service.get_account("pro")["pool_account_id"]
        route = ModelRoute(account_ids=frozenset({account_id}), allow_anonymous=False)
        with mock.patch(
            "services.model_service.model_catalog_service.route_for_model",
            return_value=route,
        ):
            tokens = {self.service.get_text_access_token(model="pro-only") for _ in range(5)}

        self.assertEqual(tokens, {"pro"})

    def test_auto_model_keeps_existing_unfiltered_rotation(self) -> None:
        with mock.patch(
            "services.model_service.model_catalog_service.route_for_model",
            side_effect=AssertionError("auto must not load the model catalog"),
        ):
            tokens = {
                self.service.get_text_access_token(model="auto"),
                self.service.get_text_access_token(model="auto"),
                self.service.get_text_access_token(model="auto"),
                self.service.get_text_access_token(model="auto"),
            }

        self.assertEqual(tokens, {"free", "plus", "pro", "pro-other"})

    def test_anonymous_model_uses_anonymous_backend(self) -> None:
        route = ModelRoute(account_ids=frozenset(), allow_anonymous=True)
        with mock.patch(
            "services.model_service.model_catalog_service.route_for_model",
            return_value=route,
        ):
            token = self.service.get_text_access_token(model="anon-only")

        self.assertEqual(token, "")

    def test_model_without_eligible_account_fails_closed(self) -> None:
        route = ModelRoute(account_ids=frozenset({"missing-internal-account-id"}), allow_anonymous=False)
        with mock.patch(
            "services.model_service.model_catalog_service.route_for_model",
            return_value=route,
        ), self.assertRaisesRegex(ModelUnavailableError, "team-only"):
            self.service.get_text_access_token(model="team-only")

    def test_disabled_advertising_account_cannot_fall_back_to_other_same_plan_account(self) -> None:
        account_id = self.service.get_account("pro-disabled")["pool_account_id"]
        route = ModelRoute(account_ids=frozenset({account_id}), allow_anonymous=False)
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route), \
                self.assertRaises(ModelUnavailableError):
            self.service.get_text_access_token(model="restricted-model")

    def test_excluded_advertising_token_does_not_route_to_other_same_plan_account(self) -> None:
        account_id = self.service.get_account("pro")["pool_account_id"]
        route = ModelRoute(account_ids=frozenset({account_id}), allow_anonymous=False)
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route), \
                self.assertRaises(ModelUnavailableError):
            self.service.get_text_access_token(excluded_tokens={"pro"}, model="restricted-model")

    def test_rotated_token_still_matches_existing_model_permission(self) -> None:
        account_id = self.service.get_account("pro")["pool_account_id"]
        self.service._apply_refreshed_tokens("pro", {"access_token": "new-pro"}, "test")
        route = ModelRoute(account_ids=frozenset({account_id}), allow_anonymous=False)
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route):
            self.assertEqual(self.service.get_text_access_token(model="restricted-model"), "new-pro")

    def test_account_disabled_during_token_refresh_uses_next_confirmed_account(self) -> None:
        identities = frozenset(self.service.get_account(token)["pool_account_id"] for token in ("pro", "pro-other"))
        route = ModelRoute(account_ids=identities, allow_anonymous=False)

        def refresh(token: str, **_kwargs) -> str:
            if token == "pro":
                self.service.update_account(token, {"status": "禁用"}, quiet=True)
            return token

        self.service.refresh_access_token = refresh
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route):
            self.assertEqual(self.service.get_text_access_token(model="restricted-model"), "pro-other")

    def test_account_deleted_during_token_refresh_fails_without_unconfirmed_fallback(self) -> None:
        route = ModelRoute(account_ids=frozenset({self.service.get_account("pro")["pool_account_id"]}),
                           allow_anonymous=False)

        def refresh(token: str, **_kwargs) -> str:
            self.service.delete_accounts([token])
            return token

        self.service.refresh_access_token = refresh
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route), \
                self.assertRaises(ModelUnavailableError):
            self.service.get_text_access_token(model="restricted-model")

    def test_channel_selection_never_borrows_other_channel_permission(self) -> None:
        web_id = self.service.get_account("free")["pool_account_id"]
        codex_id = self.service.get_account("plus")["pool_account_id"]
        route = ModelRoute(
            account_ids=frozenset({web_id, codex_id}),
            web_account_ids=frozenset({web_id}),
            codex_account_ids=frozenset({codex_id}),
        )
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route):
            self.assertEqual(self.service.get_text_access_token(model="shared-model"), "free")
            self.assertEqual(self.service.get_text_access_token(model="shared-model", channel="codex"), "plus")
            with self.assertRaises(ModelUnavailableError):
                self.service.get_text_access_token(excluded_tokens={"plus"}, model="shared-model", channel="codex")
            with self.assertRaises(ModelUnavailableError):
                self.service.get_text_access_token(excluded_tokens={"free"}, model="shared-model", channel="web")

    def test_codex_only_route_cannot_select_web_account_or_anonymous_fallback(self) -> None:
        codex_id = self.service.get_account("pro")["pool_account_id"]
        route = ModelRoute(account_ids=frozenset({codex_id}), web_account_ids=frozenset(),
                           codex_account_ids=frozenset({codex_id}), allow_anonymous=True)
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route):
            self.assertEqual(self.service.get_text_access_token(model="codex-model", channel="codex"), "pro")
            # Anonymous capability is specific to the web manifest.
            self.assertEqual(self.service.get_text_access_token(model="codex-model", channel="web"), "")
            with self.assertRaises(ModelUnavailableError):
                self.service.get_text_access_token(excluded_tokens={"pro"}, model="codex-model", channel="codex")

    def test_codex_route_without_web_capability_fails_closed_on_web(self) -> None:
        codex_id = self.service.get_account("pro")["pool_account_id"]
        route = ModelRoute(account_ids=frozenset({codex_id}), web_account_ids=frozenset(),
                           codex_account_ids=frozenset({codex_id}))
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route), \
                self.assertRaises(ModelUnavailableError):
            self.service.get_text_access_token(model="codex-model")

    def test_codex_auto_requires_explicit_upstream_permission(self) -> None:
        route = ModelRoute(account_ids=frozenset(), allow_anonymous=True)
        with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route) as catalog, \
                self.assertRaises(ModelUnavailableError):
            self.service.get_text_access_token(model="auto", channel="codex")
        catalog.assert_called_once_with("auto")

    def test_legacy_route_default_remains_web_only(self) -> None:
        route = ModelRoute(account_ids=frozenset({"legacy-account"}))
        self.assertEqual(route.account_ids_for_channel("web"), frozenset({"legacy-account"}))
        self.assertEqual(route.account_ids_for_channel("codex"), frozenset())
        with self.assertRaises(ValueError):
            route.account_ids_for_channel("unknown")


class TextProtocolRoutingTests(unittest.TestCase):
    def test_text_backend_passes_requested_model_to_account_selector(self) -> None:
        backend = mock.Mock()
        with (
            mock.patch.object(conversation.model_catalog_service, "route_for_model", return_value=ModelRoute(
                account_ids=frozenset({"synthetic-web-account"}),
            )),
            mock.patch.object(
                conversation.account_service,
                "get_text_access_token",
                return_value="pro",
            ) as selector,
            mock.patch.object(conversation, "OpenAIBackendAPI", return_value=backend),
        ):
            result = conversation.text_backend("pro-only")

        self.assertIs(result, backend)
        selector.assert_called_once_with(model="pro-only")

    def test_chat_completions_passes_requested_model_to_text_backend(self) -> None:
        body = {
            "model": "pro-chat",
            "messages": [{"role": "user", "content": "route chat"}],
        }
        with (
            mock.patch.object(openai_v1_chat_complete, "text_backend", return_value=object()) as backend,
            mock.patch.object(openai_v1_chat_complete, "collect_text", return_value="ok"),
        ):
            openai_v1_chat_complete.handle(body)

        backend.assert_called_once_with("pro-chat")

    def test_responses_passes_requested_model_to_text_backend(self) -> None:
        body = {"model": "pro-response", "input": "route response"}
        with (
            mock.patch.object(openai_v1_response, "text_backend", return_value=object()) as backend,
            mock.patch.object(openai_v1_response, "stream_text_deltas", return_value=iter(["ok"])),
        ):
            openai_v1_response.handle(body)

        backend.assert_called_once_with("pro-response")

    def test_anthropic_messages_passes_requested_model_to_text_backend(self) -> None:
        backend = object()
        with mock.patch.object(anthropic_v1_messages, "text_backend", return_value=backend) as selector:
            request = anthropic_v1_messages.message_request({
                "model": "pro-anthropic",
                "messages": [{"role": "user", "content": "route anthropic"}],
            })

        self.assertEqual(request.model, "pro-anthropic")
        self.assertIs(request.backend, backend)
        selector.assert_called_once_with("pro-anthropic")

    def test_invalid_token_retry_keeps_requested_model_filter(self) -> None:
        initial_backend = SimpleNamespace(access_token="bad")
        request = conversation.ConversationRequest(
            model="pro-only",
            messages=[{"role": "user", "content": "hello"}],
        )

        def fake_events(backend, **_kwargs):
            if backend.access_token == "bad":
                raise RuntimeError("token_invalidated")
            yield {"type": "conversation.delta", "delta": "ok"}

        with (
            mock.patch.object(conversation, "OpenAIBackendAPI", side_effect=lambda access_token: SimpleNamespace(
                access_token=access_token,
                close=lambda: None,
            )),
            mock.patch.object(conversation, "conversation_events", side_effect=fake_events),
            mock.patch.object(
                conversation.account_service,
                "refresh_access_token",
                return_value="bad",
            ),
            mock.patch.object(conversation.account_service, "remove_invalid_token"),
            mock.patch.object(
                conversation.account_service,
                "get_text_access_token",
                return_value="pro",
            ) as selector,
            mock.patch.object(conversation.account_service, "mark_text_used"),
        ):
            result = list(conversation.stream_text_deltas(initial_backend, request))

        self.assertEqual(result, ["ok"])
        selector.assert_called_once_with(
            excluded_tokens={"bad"},
            model="pro-only",
        )


if __name__ == "__main__":
    unittest.main()
