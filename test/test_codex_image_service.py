from __future__ import annotations

import base64
from io import BytesIO
import unittest
from unittest import mock

from PIL import Image

from services import codex_image_service
from services.codex_image_service import CodexImageError, build_codex_image_request, generate_codex_image, parse_codex_image_response
from services.protocol import conversation


def image_bytes(*, encoding: str = "PNG", mode: str = "RGBA", size: tuple[int, int] = (2, 2)) -> bytes:
    output = BytesIO()
    Image.new(mode, size).save(output, format=encoding)
    return output.getvalue()


def encoded_image(**options) -> str:
    return base64.b64encode(image_bytes(**options)).decode("ascii")


class CodexImagePayloadTests(unittest.TestCase):
    def test_generations_preserve_both_exact_ids_and_one_png_per_pool_item(self) -> None:
        for model in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst"):
            with self.subTest(model=model):
                path, payload = build_codex_image_request(model=model, prompt="draw", size="1024x1536", quality="high")
                self.assertEqual(path, "/backend-api/codex/images/generations")
                self.assertEqual(payload, {"model": model, "prompt": "draw", "size": "1024x1536",
                                           "quality": "high", "output_format": "png", "n": 1})
                self.assertNotIn("stream", payload)
                self.assertNotIn("images", payload)

    def test_edits_infer_real_jpeg_webp_mime_and_preserve_mask_bytes(self) -> None:
        jpeg = encoded_image(encoding="JPEG", mode="RGB")
        webp = encoded_image(encoding="WEBP", mode="RGB")
        mask = encoded_image()
        path, payload = build_codex_image_request(model="gpt-image-2.5-flare", prompt="edit", images=[jpeg, webp], mask=mask)
        self.assertEqual(path, "/backend-api/codex/images/edits")
        self.assertEqual(payload["images"], [{"image_url": "data:image/jpeg;base64," + jpeg},
                                              {"image_url": "data:image/webp;base64," + webp}])
        self.assertEqual(payload["mask"], {"image_url": "data:image/png;base64," + mask})

    def test_data_uri_declared_mime_cannot_override_actual_image_encoding(self) -> None:
        jpeg = encoded_image(encoding="JPEG", mode="RGB")
        _, payload = build_codex_image_request(model="gpt-image-2.5-sunburst", prompt="edit", images=["data:image/png;base64," + jpeg])
        self.assertEqual(payload["images"], [{"image_url": "data:image/jpeg;base64," + jpeg}])

    def test_bare_snapshot_and_unknown_model_are_explicitly_rejected(self) -> None:
        for model in ("gpt-image-2.5", "gpt-image-2.5-flare-other", "gpt-image-2", "plus-gpt-image-2.5-flare"):
            with self.subTest(model=model), self.assertRaises(CodexImageError) as caught:
                build_codex_image_request(model=model, prompt="draw")
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(caught.exception.code, "unsupported_image_model")

    def test_mask_requires_reference_image(self) -> None:
        with self.assertRaises(CodexImageError) as caught:
            build_codex_image_request(model="gpt-image-2.5-flare", prompt="edit", mask=encoded_image())
        self.assertEqual(caught.exception.code, "invalid_mask")
        self.assertEqual(caught.exception.status_code, 400)

    def test_mask_requires_png_alpha_and_matching_dimensions(self) -> None:
        for mask in (encoded_image(encoding="JPEG", mode="RGB"), encoded_image(mode="RGB"), encoded_image(size=(1, 1))):
            with self.subTest(mask=mask), self.assertRaises(CodexImageError) as caught:
                build_codex_image_request(model="gpt-image-2.5-flare", prompt="edit", images=[encoded_image()], mask=mask)
            self.assertEqual(caught.exception.code, "invalid_mask")
            self.assertEqual(caught.exception.status_code, 400)

    def test_invalid_reference_image_is_not_forwarded_as_text_or_base64_garbage(self) -> None:
        for image in ("not-base64-private", base64.b64encode(b"private text").decode(), "data:text/plain;base64,dGV4dA=="):
            with self.subTest(image=image), self.assertRaises(CodexImageError) as caught:
                build_codex_image_request(model="gpt-image-2.5-flare", prompt="edit", images=[image])
            self.assertEqual(caught.exception.code, "invalid_image_input")
            self.assertNotIn("private", str(caught.exception))


class CodexImageParserTests(unittest.TestCase):
    def test_png_base64_and_revised_prompt_are_normalized(self) -> None:
        image = encoded_image()
        self.assertEqual(parse_codex_image_response({"data": [{"b64_json": "data:image/png;base64," + image,
                                                              "revised_prompt": "revised"}]}),
                         [{"b64_json": image, "revised_prompt": "revised"}])

    def test_empty_or_malformed_output_is_not_success(self) -> None:
        for payload in (None, {}, {"data": []}, {"data": "bad"}, {"data": [None]}, {"data": [{}]},
                        {"data": [{"b64_json": "invalid-secret"}]},
                        {"data": [{"b64_json": encoded_image(), "revised_prompt": 42}]}):
            with self.subTest(payload=payload), self.assertRaises(CodexImageError) as caught:
                parse_codex_image_response(payload)
            self.assertEqual(caught.exception.status_code, 502)
            self.assertNotIn("secret", str(caught.exception))

    def test_valid_jpeg_cannot_be_returned_as_the_forced_png_output(self) -> None:
        with self.assertRaises(CodexImageError) as caught:
            parse_codex_image_response({"data": [{"b64_json": encoded_image(encoding="JPEG", mode="RGB")} ]})
        self.assertEqual(caught.exception.code, "invalid_upstream_image")

    def test_png_signature_with_truncated_pixels_is_rejected(self) -> None:
        image = image_bytes()[:-15]
        with self.assertRaises(CodexImageError):
            parse_codex_image_response({"data": [{"b64_json": base64.b64encode(image).decode()}]})

    def test_any_invalid_item_fails_the_batch_instead_of_counting_partial_bad_data_as_success(self) -> None:
        with self.assertRaises(CodexImageError):
            parse_codex_image_response({"data": [{"b64_json": encoded_image()}, {"b64_json": "not-image"}]})

    def test_upstream_error_messages_are_not_exposed(self) -> None:
        with self.assertRaises(CodexImageError) as caught:
            parse_codex_image_response({"error": {"code": "content_policy_violation", "message": "Authorization private-token"}})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.code, "content_policy_violation")
        self.assertNotIn("private-token", str(caught.exception))


class CodexImageTransportTests(unittest.TestCase):
    def make_backend(self, *, status: int = 200, payload: object = None):
        response = mock.Mock(status_code=status)
        response.json.return_value = payload if payload is not None else {"data": [{"b64_json": encoded_image()}]}
        backend = mock.Mock(access_token="synthetic-private-token", base_url="https://chatgpt.com", account={"source_type": "web", "type": "free"})
        backend.session.post.return_value = response
        return backend, response

    def test_generations_use_direct_json_and_shared_identity_without_bootstrap_or_source_restriction(self) -> None:
        backend, response = self.make_backend()
        with mock.patch.object(codex_image_service, "codex_headers", return_value={"Version": "0.147.0"}) as headers:
            result = generate_codex_image(backend, model="gpt-image-2.5-sunburst", prompt="draw")
        self.assertEqual(len(result), 1)
        args, kwargs = backend.session.post.call_args
        self.assertEqual(args, ("https://chatgpt.com/backend-api/codex/images/generations",))
        self.assertEqual(kwargs["json"]["model"], "gpt-image-2.5-sunburst")
        self.assertEqual(kwargs["json"]["n"], 1)
        self.assertEqual(kwargs["headers"], {"Version": "0.147.0"})
        headers.assert_called_once_with(backend.access_token, backend.account)
        backend._bootstrap.assert_not_called()
        response.close.assert_called_once()

    def test_edit_payload_sends_images_and_mask_without_compositing(self) -> None:
        backend, _response = self.make_backend()
        image, mask = encoded_image(), encoded_image()
        with mock.patch.object(codex_image_service, "codex_headers", return_value={}):
            generate_codex_image(backend, model="gpt-image-2.5-flare", prompt="edit", images=[image], mask=mask)
        args, kwargs = backend.session.post.call_args
        self.assertEqual(args, ("https://chatgpt.com/backend-api/codex/images/edits",))
        self.assertEqual(kwargs["json"]["mask"], {"image_url": "data:image/png;base64," + mask})
        self.assertEqual(kwargs["json"]["images"], [{"image_url": "data:image/png;base64," + image}])

    def test_http_errors_are_safe_and_close_response(self) -> None:
        for status, code in ((401, "token_invalidated"), (403, "upstream_access_denied"),
                             (404, "upstream_endpoint_not_found"), (429, "upstream_rate_limit"), (500, "upstream_http_500")):
            backend, response = self.make_backend(status=status, payload={"error": {"message": "private-token private-password"}})
            with self.subTest(status=status), mock.patch.object(codex_image_service, "codex_headers", return_value={}), \
                    self.assertRaises(CodexImageError) as caught:
                generate_codex_image(backend, model="gpt-image-2.5-flare", prompt="draw")
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.upstream_status, status)
            self.assertFalse(caught.exception.retryable_connection)
            self.assertNotIn("private", str(caught.exception))
            response.close.assert_called_once()

    def test_forbidden_detail_does_not_claim_that_a_specific_model_is_missing(self) -> None:
        backend, _ = self.make_backend(status=403, payload={"detail": "Forbidden private-token"})
        with mock.patch.object(codex_image_service, "codex_headers", return_value={}), self.assertRaises(CodexImageError) as caught:
            generate_codex_image(backend, model="gpt-image-2.5-flare", prompt="draw")
        self.assertEqual(caught.exception.code, "upstream_access_denied")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("Codex", str(caught.exception))
        self.assertIn("gpt-image-2", str(caught.exception))
        self.assertNotIn("private-token", str(caught.exception))
        backend.session.post.assert_called_once()

    def test_404_is_only_a_model_permission_error_when_upstream_explicitly_identifies_the_model(self) -> None:
        for payload, expected in (({"detail": "Not Found"}, "upstream_endpoint_not_found"),
                                  ({"error": {"code": "model_not_found", "message": "private-token"}}, "model_permission_denied")):
            backend, _ = self.make_backend(status=404, payload=payload)
            with self.subTest(expected=expected), mock.patch.object(codex_image_service, "codex_headers", return_value={}), \
                    self.assertRaises(CodexImageError) as caught:
                generate_codex_image(backend, model="gpt-image-2.5-flare", prompt="draw")
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(caught.exception.upstream_status, 404)
            self.assertNotIn("private-token", str(caught.exception))

    def test_rejected_request_parameters_remain_distinct_from_permission_denials(self) -> None:
        for status in (400, 422):
            backend, _ = self.make_backend(status=status, payload={"detail": "invalid size private-token"})
            with self.subTest(status=status), mock.patch.object(codex_image_service, "codex_headers", return_value={}), \
                    self.assertRaises(CodexImageError) as caught:
                generate_codex_image(backend, model="gpt-image-2.5-flare", prompt="draw")
            self.assertEqual(caught.exception.code, "invalid_image_request")
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(caught.exception.upstream_status, status)
            self.assertFalse(caught.exception.retryable_connection)

    def test_timeout_or_unknown_network_failure_is_safe_and_not_retryable(self) -> None:
        backend, _response = self.make_backend()
        failure = RuntimeError("private-token proxy://user:password@host")
        failure.code = 28
        backend.session.post.side_effect = failure
        with mock.patch.object(codex_image_service, "codex_headers", return_value={}), self.assertRaises(CodexImageError) as caught:
            generate_codex_image(backend, model="gpt-image-2.5-flare", prompt="draw")
        self.assertFalse(caught.exception.retryable_connection)
        self.assertNotIn("private", str(caught.exception))

    def test_pre_submit_tls_failure_can_retry_without_exposing_original_message(self) -> None:
        backend, _response = self.make_backend()
        failure = RuntimeError("private-token certificate error")
        failure.code = 35
        backend.session.post.side_effect = failure
        with mock.patch.object(codex_image_service, "codex_headers", return_value={}), self.assertRaises(CodexImageError) as caught:
            generate_codex_image(backend, model="gpt-image-2.5-flare", prompt="draw")
        self.assertTrue(caught.exception.retryable_connection)
        self.assertNotIn("private", str(caught.exception))

    def test_unexpected_image_count_does_not_consume_one_pool_item_for_two_results(self) -> None:
        backend, response = self.make_backend(payload={"data": [{"b64_json": encoded_image()}, {"b64_json": encoded_image()}]})
        with mock.patch.object(codex_image_service, "codex_headers", return_value={}), self.assertRaises(CodexImageError) as caught:
            generate_codex_image(backend, model="gpt-image-2.5-flare", prompt="draw")
        self.assertEqual(caught.exception.code, "invalid_upstream_image_count")
        response.close.assert_called_once()


class WebImage25PoolRecoveryTests(unittest.TestCase):
    def test_timeout_is_submitted_once_and_settled_once_without_legacy_logs(self) -> None:
        failure = RuntimeError("private-token read timeout")
        failure.code = 28
        request = conversation.ConversationRequest(model="gpt-image-2.5-flare", prompt="draw")
        with mock.patch.object(conversation.account_service, "get_available_access_token", return_value="private-token") as select, \
                mock.patch.object(conversation.account_service, "get_account", return_value={"pool_account_id": "account-id"}), \
                mock.patch.object(conversation.account_service, "mark_image_result") as mark, \
                mock.patch.object(conversation.account_service, "release_image_slot") as release, \
                mock.patch.object(conversation, "stream_image_outputs", side_effect=failure) as generate, \
                mock.patch.object(conversation, "OpenAIBackendAPI") as factory, \
                mock.patch.object(conversation, "logger") as log:
            with self.assertRaises(conversation.ImageGenerationError):
                list(conversation.stream_image_outputs_with_pool(request))
        select.assert_called_once_with()
        generate.assert_called_once()
        mark.assert_called_once_with("private-token", False)
        release.assert_not_called()
        factory.return_value.close.assert_called_once()
        self.assertNotIn("private-token", str(log.mock_calls))

    def test_tls_failure_retries_bounded_then_succeeds_with_exact_model(self) -> None:
        failure = RuntimeError("private-token TLS handshake failed")
        failure.code = 35
        request = conversation.ConversationRequest(model="gpt-image-2.5-sunburst", prompt="draw")
        with mock.patch.object(conversation.account_service, "get_available_access_token", return_value="private-token"), \
                mock.patch.object(conversation.account_service, "get_account", return_value={"pool_account_id": "account-id"}), \
                mock.patch.object(conversation.account_service, "mark_image_result") as mark, \
                mock.patch.object(conversation, "stream_image_outputs", side_effect=[failure, [conversation.ImageOutput(kind="result", model=request.model, index=1, total=1, data=[{"b64_json": encoded_image()}])]]) as generate, \
                mock.patch.object(conversation, "OpenAIBackendAPI"), \
                mock.patch.object(conversation.time, "sleep"):
            results = list(conversation.stream_image_outputs_with_pool(request))
        self.assertEqual(len(results), 1)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual([call.args[1].model for call in generate.call_args_list], [request.model, request.model])
        self.assertEqual(mark.call_args_list, [mock.call("private-token", False), mock.call("private-token", True)])


if __name__ == "__main__":
    unittest.main()
