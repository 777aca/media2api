from __future__ import annotations

import unittest
from io import BytesIO
from unittest import mock

from PIL import Image
from services.config import config
from services.openai_backend_api import OpenAIBackendAPI, ImagePollTimeoutError
from services.protocol import conversation, openai_v1_image_edit
from utils.helper import UpstreamHTTPError


MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")


class WebImage25RequestTests(unittest.TestCase):
    def test_web_mask_preserves_alpha_for_both_rgba_and_grayscale_alpha_png(self) -> None:
        original = BytesIO()
        Image.new("RGB", (2, 2), "red").save(original, format="PNG")
        for mode, color in (("RGBA", (0, 0, 0, 127)), ("LA", (0, 127))):
            with self.subTest(mode=mode):
                mask = BytesIO()
                Image.new(mode, (2, 2), color).save(mask, format="PNG")
                result = openai_v1_image_edit._composite_mask(
                    [(original.getvalue(), "reference.png", "image/png")],
                    [(mask.getvalue(), "mask.png", "image/png")], validate_masks=True)
                with Image.open(BytesIO(result[0][0])) as image:
                    self.assertEqual(image.getchannel("A").getextrema(), (127, 127))
                    self.assertEqual(image.getpixel((0, 0))[:3], (255, 0, 0))

    def test_web_mask_rejects_missing_alpha_or_wrong_size(self) -> None:
        original = BytesIO()
        Image.new("RGB", (2, 2)).save(original, format="PNG")
        for mode, size in (("RGB", (2, 2)), ("RGBA", (3, 3))):
            mask = BytesIO()
            Image.new(mode, size).save(mask, format="PNG")
            with self.subTest(mode=mode, size=size), self.assertRaises(conversation.ImageGenerationError) as caught:
                openai_v1_image_edit._composite_mask(
                    [(original.getvalue(), "reference.png", "image/png")],
                    [(mask.getvalue(), "mask.png", "image/png")], validate_masks=True)
            self.assertEqual(caught.exception.code, "invalid_mask")

    def test_prepare_and_generation_send_exact_image_model_without_thinking_override(self) -> None:
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.base_url = "https://example.invalid"
        backend.session = mock.Mock()
        response = backend.session.post.return_value
        response.status_code = 200
        response.json.return_value = {"conduit_token": "synthetic-conduit"}
        with mock.patch.object(backend, "_image_headers", return_value={}), \
                mock.patch.dict(config.data, {"default_upstream_model_name": "gpt-5-5-extended",
                                              "default_thinking_effort": "max"}):
            for model in MODELS:
                with self.subTest(model=model):
                    backend.session.post.reset_mock()
                    conduit = backend._prepare_image_conversation("draw", mock.Mock(), model)
                    backend._start_image_generation("draw", mock.Mock(), conduit, model)
                    calls = backend.session.post.call_args_list
                    self.assertEqual([call.args[0] for call in calls], [
                        "https://example.invalid/backend-api/f/conversation/prepare",
                        "https://example.invalid/backend-api/f/conversation",
                    ])
                    for call in calls:
                        payload = call.kwargs["json"]
                        self.assertEqual(payload["model"], model)
                        self.assertEqual(payload["system_hints"], ["picture_v2"])
                        self.assertNotIn("thinking_effort", payload)
            self.assertEqual(backend._image_model_settings("gpt-image-2"), ("gpt-5-5", "extended"))
            self.assertEqual(backend._image_model_settings("codex-gpt-image-2")[0], "codex-gpt-image-2")

    def test_http_errors_keep_safe_status_and_distinguish_explicit_model_denial(self) -> None:
        for status, payload, code in (
            (403, {"detail": "private-token"}, "upstream_access_denied"),
            (404, {"detail": "private-token"}, "upstream_http_404"),
            (404, {"error": {"code": "model_not_found", "message": "private-token"}}, "model_permission_denied"),
            (403, {"error": {"code": {"unexpected": "private-token"}}}, "upstream_access_denied"),
            (429, {}, "upstream_rate_limit"),
            (422, {}, "invalid_image_request"),
        ):
            with self.subTest(status=status, code=code):
                result = conversation._web_image_25_error(UpstreamHTTPError("/backend-api/f/conversation", status, payload))
                self.assertEqual(result.code, code)
                self.assertEqual(result.upstream_status, status)
                self.assertNotIn("private-token", str(result.to_openai_error()))
                self.assertNotIn("Codex", str(result))

    def test_result_stage_failures_never_resubmit_generation(self) -> None:
        for failure in (ImagePollTimeoutError("private-token", "conversation-id"),
                        UpstreamHTTPError("image_download", 401, {"detail": "private-token"})):
            with self.subTest(error=type(failure).__name__):
                backend = mock.Mock()

                def fail(backend, *_args):
                    from services.generation_context import checkpoint
                    checkpoint("submitted", conversation_id="conversation-id")
                    raise failure

                with mock.patch.object(conversation, "OpenAIBackendAPI", return_value=backend), \
                        mock.patch.object(conversation, "stream_image_outputs", side_effect=fail) as generate, \
                        mock.patch.object(conversation, "_remove_image_conversation_later"), \
                        mock.patch.object(conversation.account_service, "acquire_governed_image_token", return_value="private-token"), \
                        mock.patch.object(conversation.account_service, "get_account", return_value={}), \
                        mock.patch.object(conversation.account_service, "refresh_access_token") as refresh, \
                        mock.patch.object(conversation.account_service, "mark_image_result") as settle:
                    with self.assertRaises(conversation.ImageGenerationError) as caught:
                        conversation._generate_web_image_25(conversation.ConversationRequest(model=MODELS[0], prompt="draw"), 1, 1)
                    self.assertNotIn("private-token", str(caught.exception))
                    generate.assert_called_once()
                    refresh.assert_not_called()
                    settle.assert_called_once_with("private-token", False)
                    backend.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
