from __future__ import annotations

import json
import unittest
from unittest import mock

from services import codex_text_service
from services.codex_text_service import (
    CodexTextBackend, CodexTextError, build_codex_text_payload, codex_text_deltas, iter_codex_sse_events,
)
from services.protocol import conversation
from services.model_service import ModelRoute


def completed(text: str) -> dict[str, object]:
    return {"type": "response.completed", "response": {
        "status": "completed", "error": None,
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
    }}


def sse(events: list[dict[str, object]]) -> list[bytes]:
    lines: list[bytes] = []
    for event in events:
        lines.extend([("data: " + json.dumps(event, ensure_ascii=False)).encode("utf-8"), b""])
    return lines


class CodexTextPayloadTests(unittest.TestCase):
    def test_payload_preserves_instructions_history_images_and_exact_model(self) -> None:
        payload = build_codex_text_payload([
            {"role": "system", "content": "be concise"},
            {"role": "developer", "content": [{"type": "text", "text": "use Chinese"}]},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": [{"type": "text", "text": "describe this"},
                                          {"type": "image", "data": b"test-image", "mime": "image/png"}]},
        ], "gpt-6-sol", "extended")
        self.assertEqual(payload["model"], "gpt-6-sol")
        self.assertEqual(payload["instructions"], "be concise\n\nuse Chinese")
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["reasoning"], {"effort": "xhigh"})
        self.assertEqual([item["role"] for item in payload["input"]], ["user", "assistant", "user"])
        self.assertEqual(payload["input"][1]["content"], [{"type": "output_text", "text": "previous answer"}])
        self.assertEqual(payload["input"][2]["content"][1], {
            "type": "input_image", "image_url": "data:image/png;base64,dGVzdC1pbWFnZQ==",
        })
        self.assertNotIn("tools", payload)

    def test_instructions_always_present_without_inventing_system_prompt(self) -> None:
        payload = build_codex_text_payload([{"role": "user", "content": "hi"}], "gpt-6-sol")
        self.assertEqual(payload["instructions"], "")
        self.assertNotIn("reasoning", payload)

    def test_unsupported_role_or_content_is_explicit_error(self) -> None:
        for message in (
            {"role": "tool", "content": "output"},
            {"role": "user", "content": [{"type": "input_audio", "data": "private"}]},
            {"role": "assistant", "content": [{"type": "image", "data": b"image", "mime": "image/png"}]},
            {"role": ["user"], "content": "text"},
        ):
            with self.subTest(message=message), self.assertRaises(CodexTextError) as caught:
                build_codex_text_payload([message], "gpt-6-sol")
            self.assertEqual(caught.exception.code, "unsupported_message_content")
            self.assertNotIn("private", str(caught.exception))


class CodexTextStreamTests(unittest.TestCase):
    def test_parser_supports_multiline_data_and_final_frame_without_blank_line(self) -> None:
        lines = [b": ping", b"event: response.output_text.delta", b'data: {"type":"response.output_text.delta",',
                 b'data: "delta":"hello"}', b"", ("data: " + json.dumps(completed("hello"))).encode()]
        self.assertEqual(list(codex_text_deltas(iter_codex_sse_events(lines))), ["hello"])

    def test_deltas_emit_before_terminal_event_is_consumed(self) -> None:
        consumed: list[str] = []

        def events():
            consumed.append("delta")
            yield {"type": "response.output_text.delta", "delta": "hello"}
            consumed.append("completion")
            yield completed("hello world")

        stream = codex_text_deltas(events())
        self.assertEqual(next(stream), "hello")
        self.assertEqual(consumed, ["delta"])
        self.assertEqual(list(stream), [" world"])

    def test_completed_output_is_used_when_upstream_has_no_delta_events(self) -> None:
        self.assertEqual(list(codex_text_deltas([completed("complete answer")])), ["complete answer"])

    def test_reasoning_is_not_exposed_or_counted_as_visible_text(self) -> None:
        final = completed("visible")
        final["response"]["output"].insert(0, {"type": "reasoning", "summary": [{"text": "private reasoning"}]})
        events = [{"type": "response.reasoning_text.delta", "delta": "private reasoning"}, final]
        self.assertEqual(list(codex_text_deltas(events)), ["visible"])

    def test_failed_incomplete_cancelled_and_error_never_complete(self) -> None:
        for event_type in ("error", "response.failed", "response.incomplete", "response.cancelled", "response.canceled"):
            event = {"type": event_type, "error": {"message": "access-token-secret", "code": "unexpected"}}
            with self.subTest(event_type=event_type), self.assertRaises(CodexTextError) as caught:
                list(codex_text_deltas([event, completed("must not return")]))
            self.assertEqual(caught.exception.code, "upstream_response_failed")
            self.assertNotIn("access-token-secret", str(caught.exception))

    def test_truncated_stream_and_done_sentinel_do_not_imply_success(self) -> None:
        for lines in (sse([{"type": "response.output_text.delta", "delta": "partial"}]),
                      [b"data: [DONE]", b""]):
            with self.subTest(lines=lines), self.assertRaises(CodexTextError) as caught:
                list(codex_text_deltas(iter_codex_sse_events(lines)))
            self.assertEqual(caught.exception.code, "incomplete_upstream_response")

    def test_malformed_payload_is_safe_error(self) -> None:
        for line in (b"data: {not json access-token-secret}", b"data: []", b"data: {}", b"data: \xff"):
            with self.subTest(line=line), self.assertRaises(CodexTextError) as caught:
                list(iter_codex_sse_events([line, b""]))
            self.assertEqual(caught.exception.code, "invalid_upstream_response")
            self.assertNotIn("access-token-secret", str(caught.exception))

    def test_final_text_mismatch_is_error(self) -> None:
        with self.assertRaises(CodexTextError) as caught:
            list(codex_text_deltas([{"type": "response.output_text.delta", "delta": "partial"}, completed("different")]))
        self.assertEqual(caught.exception.code, "invalid_upstream_response")

    def test_empty_completed_response_is_error(self) -> None:
        with self.assertRaises(CodexTextError) as caught:
            list(codex_text_deltas([completed("")]))
        self.assertEqual(caught.exception.code, "empty_upstream_response")

    def test_refusal_and_unexpected_tool_output_are_explicit_error(self) -> None:
        final = completed("ignored")
        final["response"]["output"] = [{"type": "function_call", "name": "tool", "arguments": "private"}]
        for event in ({"type": "response.refusal.delta", "delta": "private"}, final):
            with self.subTest(event=event), self.assertRaises(CodexTextError) as caught:
                list(codex_text_deltas([event]))
            self.assertNotIn("private", str(caught.exception))

    def test_token_error_code_supports_existing_refresh_without_exposing_body(self) -> None:
        event = {"type": "response.failed", "response": {"error": {"code": "token_revoked", "message": "private"}}}
        with self.assertRaises(CodexTextError) as caught:
            list(codex_text_deltas([event]))
        self.assertTrue(conversation.is_token_invalid_error(str(caught.exception)))
        self.assertNotIn("private", str(caught.exception))


class CodexTextTransportTests(unittest.TestCase):
    def make_backend(self, *, status: int = 200, lines: list[bytes] | None = None) -> tuple[CodexTextBackend, mock.Mock, mock.Mock]:
        response = mock.Mock(status_code=status, headers={"content-type": "text/event-stream"})
        response.iter_lines.return_value = iter(lines if lines is not None else sse([completed("ok")]))
        transport = mock.Mock(base_url="https://chatgpt.com", account={"pool_account_id": "synthetic-id"})
        transport.session.post.return_value = response
        backend = CodexTextBackend("synthetic-token")
        backend._backend = transport
        return backend, transport, response

    def test_backend_creates_no_unused_network_session_before_streaming(self) -> None:
        with mock.patch.object(codex_text_service, "OpenAIBackendAPI") as factory:
            backend = CodexTextBackend("synthetic-token")
            backend.close()
        factory.assert_not_called()

    def test_transport_uses_exact_codex_endpoint_stream_and_shared_headers(self) -> None:
        backend, transport, response = self.make_backend()
        headers = {"Authorization": "Bearer synthetic-token", "Version": "0.146.0"}
        with mock.patch.object(codex_text_service, "codex_headers", return_value=headers) as header_factory:
            self.assertEqual(list(backend.iter_text_deltas([{"role": "user", "content": "hi"}], "gpt-6-sol")), ["ok"])
        args, kwargs = transport.session.post.call_args
        self.assertEqual(args, ("https://chatgpt.com/backend-api/codex/responses",))
        self.assertEqual(kwargs["headers"], headers)
        self.assertEqual(kwargs["json"]["model"], "gpt-6-sol")
        self.assertTrue(kwargs["stream"])
        header_factory.assert_called_once_with("synthetic-token", transport.account, accept="text/event-stream")
        transport._bootstrap.assert_not_called()
        response.close.assert_called_once()

    def test_http_failure_does_not_read_or_expose_upstream_body(self) -> None:
        for status, code in ((401, "token_invalidated"), (403, "upstream_http_403"), (429, "upstream_rate_limit"), (500, "upstream_http_500")):
            backend, _transport, response = self.make_backend(status=status)
            response.text = "access-token-secret"
            with mock.patch.object(codex_text_service, "codex_headers", return_value={}), \
                    self.subTest(status=status), self.assertRaises(CodexTextError) as caught:
                list(backend.iter_text_deltas([{"role": "user", "content": "hi"}], "gpt-6-sol"))
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("access-token-secret", str(caught.exception))
            response.iter_lines.assert_not_called()
            response.close.assert_called_once()

    def test_network_errors_are_sanitized(self) -> None:
        backend, transport, _response = self.make_backend()
        transport.session.post.side_effect = RuntimeError("proxy://user:password@example Authorization synthetic-token")
        with mock.patch.object(codex_text_service, "codex_headers", return_value={}), self.assertRaises(CodexTextError) as caught:
            list(backend.iter_text_deltas([{"role": "user", "content": "hi"}], "gpt-6-sol"))
        self.assertEqual(caught.exception.code, "upstream_connection_failed")
        self.assertNotIn("password", str(caught.exception))
        self.assertNotIn("synthetic-token", str(caught.exception))

    def test_unexpected_non_sse_response_is_explicit_error(self) -> None:
        backend, _transport, response = self.make_backend()
        response.headers = {"content-type": "application/json"}
        with mock.patch.object(codex_text_service, "codex_headers", return_value={}), self.assertRaises(CodexTextError) as caught:
            list(backend.iter_text_deltas([{"role": "user", "content": "hi"}], "gpt-6-sol"))
        self.assertEqual(caught.exception.code, "invalid_upstream_response")
        response.close.assert_called_once()


class CodexTextProtocolTests(unittest.TestCase):
    def test_codex_only_model_selects_confirmed_codex_account_and_lazy_backend(self) -> None:
        route = ModelRoute(account_ids=frozenset({"account-id"}), web_account_ids=frozenset(),
                           codex_account_ids=frozenset({"account-id"}))
        with mock.patch.object(conversation.model_catalog_service, "route_for_model", return_value=route), \
                mock.patch.object(conversation.account_service, "get_text_access_token", return_value="synthetic-token") as selector, \
                mock.patch.object(codex_text_service, "OpenAIBackendAPI") as web_factory:
            backend = conversation.text_backend("gpt-6-sol")
        self.assertIsInstance(backend, CodexTextBackend)
        selector.assert_called_once_with(model="gpt-6-sol", channel="codex")
        web_factory.assert_not_called()

    def test_model_shared_by_both_channels_keeps_web_priority(self) -> None:
        route = ModelRoute(account_ids=frozenset({"web", "codex"}), web_account_ids=frozenset({"web"}),
                           codex_account_ids=frozenset({"codex"}))
        expected = object()
        with mock.patch.object(conversation.model_catalog_service, "route_for_model", return_value=route), \
                mock.patch.object(conversation.account_service, "get_text_access_token", return_value="web-token") as selector, \
                mock.patch.object(conversation, "OpenAIBackendAPI", return_value=expected):
            self.assertIs(conversation.text_backend("shared-model"), expected)
        selector.assert_called_once_with(model="shared-model")

    def test_codex_text_aggregation_uses_codex_and_marks_account_after_completion(self) -> None:
        backend = CodexTextBackend("synthetic-token")
        request = conversation.ConversationRequest(model="gpt-6-sol", prompt="hi", thinking_effort="high")
        with mock.patch.object(CodexTextBackend, "iter_text_deltas", return_value=iter(["hello", " world"])) as stream, \
                mock.patch.object(conversation, "conversation_events") as web_stream, \
                mock.patch.object(conversation.account_service, "mark_text_used") as mark:
            self.assertEqual(conversation.collect_text(backend, request), "hello world")
        web_stream.assert_not_called()
        mark.assert_called_once_with("synthetic-token")
        messages, model, effort = stream.call_args.args
        self.assertEqual(messages[-1], {"role": "user", "content": "hi"})
        self.assertEqual((model, effort), ("gpt-6-sol", "high"))

    def test_invalid_codex_token_retry_never_switches_to_unconfirmed_web_channel(self) -> None:
        backend = CodexTextBackend("bad-token")
        request = conversation.ConversationRequest(model="gpt-6-sol", prompt="hi")

        def stream(active, *_args):
            if active.access_token == "bad-token":
                raise CodexTextError("The upstream authentication token has been invalidated.", "token_invalidated")
            yield "ok"

        with mock.patch.object(CodexTextBackend, "iter_text_deltas", stream), \
                mock.patch.object(conversation, "conversation_events") as web_stream, \
                mock.patch.object(conversation.account_service, "refresh_access_token", return_value="bad-token"), \
                mock.patch.object(conversation.account_service, "remove_invalid_token"), \
                mock.patch.object(conversation.account_service, "get_text_access_token", return_value="confirmed-codex-token") as selector, \
                mock.patch.object(conversation.account_service, "mark_text_used") as mark:
            self.assertEqual(list(conversation.stream_text_deltas(backend, request)), ["ok"])
        selector.assert_called_once_with(excluded_tokens={"bad-token"}, model="gpt-6-sol", channel="codex")
        mark.assert_called_once_with("confirmed-codex-token")
        web_stream.assert_not_called()

    def test_failure_after_partial_text_is_not_marked_success_or_resubmitted(self) -> None:
        backend = CodexTextBackend("synthetic-token")
        request = conversation.ConversationRequest(model="gpt-6-sol", prompt="hi")

        def stream(_active, *_args):
            yield "partial"
            raise CodexTextError("The upstream authentication token has been invalidated.", "token_invalidated")

        with mock.patch.object(CodexTextBackend, "iter_text_deltas", stream), \
                mock.patch.object(conversation.account_service, "refresh_access_token") as refresh, \
                mock.patch.object(conversation.account_service, "mark_text_used") as mark:
            result = conversation.stream_text_deltas(backend, request)
            self.assertEqual(next(result), "partial")
            with self.assertRaises(CodexTextError):
                next(result)
        refresh.assert_not_called()
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
