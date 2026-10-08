from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
from threading import Barrier, Lock
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import api.ai as ai_module
import api.support as support_module
from services.account_service import AccountService
from services import codex_image_service
from utils.helper import UpstreamHTTPError
from services.config import config
from services.protocol import conversation, openai_v1_chat_complete, openai_v1_response
from services.storage.json_storage import JSONStorageBackend
from utils.helper import is_web_image_model_25, is_image_chat_request, is_supported_image_model, split_image_model


WEB_MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")


def png_base64(color: tuple[int, int, int, int] = (255, 0, 0, 255)) -> str:
    output = BytesIO()
    Image.new("RGBA", (2, 2), color).save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


class WebImage25ModelClassificationTests(unittest.TestCase):
    def test_complete_model_ids_are_supported_image_models(self) -> None:
        for model in WEB_MODELS:
            with self.subTest(model=model):
                self.assertTrue(is_supported_image_model(model))
                self.assertTrue(is_web_image_model_25(model))
                self.assertEqual(split_image_model(model), (None, model))
                self.assertTrue(is_image_chat_request({"model": model}))
                self.assertFalse(openai_v1_response.is_text_response_request({"model": model, "input": "Draw a cat"}))

    def test_bare_or_plan_prefixed_25_ids_are_not_supported(self) -> None:
        for model in ("gpt-image-2.5", "plus-gpt-image-2.5-flare", "codex-gpt-image-2.5", "gpt-image-2.5-other"):
            with self.subTest(model=model):
                self.assertFalse(is_supported_image_model(model))
                self.assertFalse(is_web_image_model_25(model))
                self.assertEqual(split_image_model(model), (None, None))


class WebImage25Fixture:
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="media2api-web-image-tests-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(AccountService, "_get_cumulative_file", return_value=self.path / ".total").start()
        mock.patch("services.account_service.log_service.add").start()
        mock.patch.dict(config.data, {"image_parallel_generation": True, "image_account_concurrency": 3,
                                      "auto_remove_rate_limited_accounts": False}).start()
        self.accounts = AccountService(JSONStorageBackend(self.path / "accounts.json"))
        self.token = "synthetic-private-access-token"
        self.accounts.add_account_items([{"access_token": self.token, "type": "free", "source_type": "web",
                                         "status": "正常", "quota": 8}])
        self.accounts.fetch_remote_info = lambda token, *_args, **_kwargs: self.accounts.get_account(token)
        mock.patch.object(conversation, "account_service", self.accounts).start()
        self.backend = mock.Mock()
        self.create_backend = mock.patch.object(conversation, "OpenAIBackendAPI", return_value=self.backend).start()
        self.image_b64 = png_base64()
        self.generate = mock.patch.object(conversation, "stream_image_outputs", side_effect=self.web_outputs).start()
        self.old_direct = mock.patch.object(codex_image_service, "generate_codex_image", side_effect=AssertionError("direct image route used")).start()
        self.old_codex = mock.patch.object(conversation, "stream_codex_image_outputs", side_effect=AssertionError("legacy Codex image route used")).start()
        mock.patch.object(conversation, "_remove_image_conversation_later").start()

    def web_outputs(self, _backend, request, index=1, total=1):
        return [conversation.ImageOutput(kind="result", model=request.model, index=index, total=total,
                                         data=[{"b64_json": self.image_b64}], conversation_id="synthetic-conversation")]

    def request(self, *, n: int = 1, model: str = WEB_MODELS[0]) -> conversation.ConversationRequest:
        return conversation.ConversationRequest(model=model, prompt="Draw a cat", n=n)


class WebImage25PoolIntegrationTests(WebImage25Fixture, unittest.TestCase):
    def test_success_uses_explicit_model_and_consumes_one_quota_and_slot(self) -> None:
        for model in WEB_MODELS:
            with self.subTest(model=model), mock.patch.object(self.accounts, "release_image_slot", wraps=self.accounts.release_image_slot) as release:
                initial = self.accounts.get_account(self.token)["quota"]
                outputs = list(conversation.stream_image_outputs_with_pool(self.request(model=model)))
                self.assertEqual(len([item for item in outputs if item.kind == "result"]), 1)
                self.assertEqual(outputs[-1].model, model)
                self.assertEqual(self.generate.call_args.args[1].model, model)
                self.assertEqual(self.accounts.get_account(self.token)["quota"], initial - 1)
                self.assertEqual(self.accounts.list_accounts()[0]["image_inflight"], 0)
                release.assert_called_once_with(self.token)
        self.old_direct.assert_not_called()
        self.old_codex.assert_not_called()

    def test_upstream_failure_releases_slot_without_consuming_quota(self) -> None:
        self.generate.side_effect = UpstreamHTTPError("/backend-api/f/conversation", 429, {"detail": "private-token"})
        with mock.patch.object(self.accounts, "release_image_slot", wraps=self.accounts.release_image_slot) as release:
            with self.assertRaises(conversation.ImageGenerationError) as caught:
                list(conversation.stream_image_outputs_with_pool(self.request()))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.code, "upstream_rate_limit")
        self.assertEqual(self.accounts.get_account(self.token)["quota"], 8)
        self.assertEqual(self.accounts.list_accounts()[0]["image_inflight"], 0)
        self.assertEqual(self.accounts.get_account(self.token)["fail"], 1)
        release.assert_called_once_with(self.token)
        self.generate.assert_called_once()
        self.old_direct.assert_not_called()
        self.old_codex.assert_not_called()

    def test_parallel_three_images_release_exactly_three_slots(self) -> None:
        started = Barrier(3)

        def generate(*args):
            started.wait(timeout=3)
            return self.web_outputs(*args)

        self.generate.side_effect = generate
        with mock.patch.object(self.accounts, "release_image_slot", wraps=self.accounts.release_image_slot) as release:
            outputs = list(conversation.stream_image_outputs_with_pool(self.request(n=3)))
        self.assertEqual(len([item for item in outputs if item.kind == "result"]), 3)
        self.assertEqual(self.accounts.get_account(self.token)["quota"], 5)
        self.assertEqual(self.accounts.get_account(self.token)["success"], 3)
        self.assertEqual(self.accounts.list_accounts()[0]["image_inflight"], 0)
        self.assertEqual(release.call_count, 3)

    def test_parallel_partial_failure_preserves_successes_and_only_charges_successes(self) -> None:
        started = Barrier(3)
        counter_lock = Lock()
        count = 0

        def generate(*args):
            nonlocal count
            with counter_lock:
                index = count
                count += 1
            started.wait(timeout=3)
            if index == 0:
                raise UpstreamHTTPError("/backend-api/f/conversation", 502, {})
            return self.web_outputs(*args)

        self.generate.side_effect = generate
        outputs = list(conversation.stream_image_outputs_with_pool(self.request(n=3)))
        self.assertEqual(len([item for item in outputs if item.kind == "result"]), 2)
        account = self.accounts.get_account(self.token)
        self.assertEqual(account["quota"], 6)
        self.assertEqual(account["success"], 2)
        self.assertEqual(account["fail"], 1)
        self.assertEqual(self.accounts.list_accounts()[0]["image_inflight"], 0)

    def test_unexpected_failure_is_safe_and_does_not_fall_back(self) -> None:
        self.generate.side_effect = RuntimeError("synthetic-private-access-token private-password private-totp")
        with mock.patch.object(conversation.logger, "warning") as warning, \
                mock.patch.object(conversation.logger, "debug") as debug:
            with self.assertRaises(conversation.ImageGenerationError) as caught:
                list(conversation.stream_image_outputs_with_pool(self.request()))
        serialized = json.dumps(caught.exception.to_openai_error()) + str(warning.call_args_list) + str(debug.call_args_list)
        for secret in (self.token, "private-password", "private-totp"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.accounts.get_account(self.token)["quota"], 8)
        self.assertEqual(self.accounts.list_accounts()[0]["image_inflight"], 0)
        self.generate.assert_called_once()
        self.old_direct.assert_not_called()
        self.old_codex.assert_not_called()

    def test_no_quota_rejects_before_upstream(self) -> None:
        self.accounts.update_account(self.token, {"quota": 0}, quiet=True)
        with self.assertRaises(conversation.ImageGenerationError):
            list(conversation.stream_image_outputs_with_pool(self.request()))
        self.generate.assert_not_called()
        self.create_backend.assert_not_called()


class WebImage25ApiIntegrationTests(WebImage25Fixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        mock.patch.object(support_module, "config", SimpleNamespace(auth_key="synthetic-admin-key", base_url="")).start()
        mock.patch.object(ai_module, "filter_or_log", new=mock.AsyncMock()).start()
        self.call_log = mock.patch.object(ai_module.LoggedCall, "log").start()
        mock.patch.object(openai_v1_chat_complete, "text_backend", side_effect=AssertionError("image model routed to text")).start()
        mock.patch.object(openai_v1_response, "text_backend", side_effect=AssertionError("image model routed to text")).start()
        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer synthetic-admin-key"}

    def test_generations_chat_and_responses_use_exact_explicit_image_id(self) -> None:
        for model in WEB_MODELS:
            for path, payload in (
                ("/v1/images/generations", {"prompt": "Draw a cat"}),
                ("/v1/chat/completions", {"messages": [{"role": "user", "content": "Draw a cat"}]}),
                ("/v1/responses", {"input": "Draw a cat"}),
            ):
                with self.subTest(model=model, path=path):
                    response = self.client.post(path, headers=self.headers, json={**payload, "model": model})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(self.generate.call_args.args[1].model, model)
        self.assertEqual(self.generate.call_count, 6)
        self.assertEqual(self.accounts.get_account(self.token)["quota"], 2)

    def test_bare_25_model_rejects_without_generating_or_defaulting(self) -> None:
        for path, payload in (
            ("/v1/images/generations", {"prompt": "Draw a cat"}),
            ("/v1/chat/completions", {"messages": [{"role": "user", "content": "Draw a cat"}]}),
            ("/v1/responses", {"input": "Draw a cat"}),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, headers=self.headers, json={**payload, "model": "gpt-image-2.5"})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("unsupported", response.text.lower())
        self.generate.assert_not_called()
        self.create_backend.assert_not_called()

    def test_access_denial_preserves_http_status_and_logs_it_without_fallback_or_quota_charge(self) -> None:
        self.generate.side_effect = UpstreamHTTPError("/backend-api/f/conversation", 403, {"detail": "private-token"})
        for path, payload in (
            ("/v1/images/generations", {"prompt": "Draw a cat"}),
            ("/v1/chat/completions", {"messages": [{"role": "user", "content": "Draw a cat"}]}),
            ("/v1/responses", {"input": "Draw a cat"}),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, headers=self.headers, json={**payload, "model": WEB_MODELS[0]})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "upstream_access_denied")
                self.assertEqual(self.call_log.call_args.kwargs["upstream_status"], 403)
                self.assertEqual(self.call_log.call_args.kwargs["error_code"], "upstream_access_denied")
        self.assertEqual(self.generate.call_count, 3)
        self.assertEqual(self.accounts.get_account(self.token)["quota"], 8)
        self.assertEqual(self.accounts._image_inflight.get(self.token, 0), 0)
        self.old_direct.assert_not_called()
        self.old_codex.assert_not_called()

    def test_edit_composites_mask_into_first_reference_only(self) -> None:
        mask_b64 = png_base64((0, 0, 0, 0))
        response = self.client.post("/v1/images/edits", headers=self.headers, json={
            "model": WEB_MODELS[0], "prompt": "Replace the background",
            "images": [{"b64_json": self.image_b64}, {"b64_json": self.image_b64}], "mask": {"b64_json": mask_b64},
        })
        self.assertEqual(response.status_code, 200, response.text)
        request = self.generate.call_args.args[1]
        self.assertIsNone(request.mask)
        self.assertEqual(request.images[1], self.image_b64)
        first_image = Image.open(BytesIO(base64.b64decode(request.images[0])))
        self.assertEqual(first_image.getchannel("A").getextrema(), (0, 0))

    def test_multiple_edit_masks_reject_before_upstream(self) -> None:
        response = self.client.post("/v1/images/edits", headers=self.headers, json={
            "model": WEB_MODELS[0], "prompt": "Replace the background",
            "images": [{"b64_json": self.image_b64}], "mask": [{"b64_json": self.image_b64}] * 2,
        })
        self.assertEqual(response.status_code, 400, response.text)
        self.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
