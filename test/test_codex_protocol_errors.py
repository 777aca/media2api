from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from services.codex_text_service import CodexTextError
from services.log_service import LoggedCall, LogService, _protocol_error_response
from services.protocol.conversation import ImageGenerationError


class CodexProtocolErrorTests(unittest.TestCase):
    def test_openai_invalid_content_preserves_400_and_safe_error_code(self) -> None:
        response = _protocol_error_response(
            CodexTextError("Unsupported content.", "unsupported_message_content", 400), 502, "openai",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["code"], "unsupported_message_content")

    def test_anthropic_rate_limit_preserves_protocol_and_429(self) -> None:
        response = _protocol_error_response(
            CodexTextError("Usage limit reached.", "upstream_rate_limit", 429), 502, "anthropic",
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(json.loads(response.body)["type"], "error")

    def test_unrelated_exception_keeps_existing_mapping(self) -> None:
        response = _protocol_error_response(RuntimeError("Existing failure."), 502, "openai")
        self.assertEqual(response.status_code, 502)


class ImageFailureLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="media2api-image-error-log-")
        self.addCleanup(directory.cleanup)
        self.logs = LogService(Path(directory.name) / "calls.jsonl")
        patch = mock.patch("services.log_service.log_service", self.logs)
        patch.start()
        self.addCleanup(patch.stop)
        self.call = LoggedCall({"id": "test-admin", "role": "admin"}, "/v1/images/generations",
                               "gpt-image-2.5-flare", "test")

    @staticmethod
    def failure() -> ImageGenerationError:
        return ImageGenerationError("Codex image access denied.", status_code=403, code="upstream_access_denied",
                                    error_type="permission_error", upstream_status=403)

    async def test_early_handler_failure_persists_safe_upstream_metadata(self) -> None:
        def handler():
            raise self.failure()
        response = await self.call.run(handler)
        self.assertEqual(response.status_code, 403)
        detail = self.logs.list()[0]["detail"]
        self.assertEqual(detail["upstream_status"], 403)
        self.assertEqual(detail["error_code"], "upstream_access_denied")

    def test_late_stream_failure_persists_metadata_and_keeps_the_error(self) -> None:
        def items():
            yield {"status": "in_progress"}
            raise self.failure()
        with self.assertRaises(ImageGenerationError):
            list(self.call.stream(items()))
        records = self.logs.list()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["detail"]["upstream_status"], 403)
        self.assertEqual(records[0]["detail"]["error_code"], "upstream_access_denied")

    def test_untrusted_diagnostic_fields_are_not_written(self) -> None:
        self.call.log("failed", status="failed", error="safe error",
                      error_code="Authorization: Bearer private-token", upstream_status="private-token")
        detail = self.logs.list()[0]["detail"]
        self.assertNotIn("upstream_status", detail)
        self.assertNotIn("error_code", detail)
        self.assertNotIn("private-token", json.dumps(detail))
