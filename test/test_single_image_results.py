from __future__ import annotations

import base64
from io import BytesIO
import unittest
from unittest import mock

from PIL import Image

from services.generation_context import ExecutionCheckpoint, execution_checkpoint
from services.generation_execution import execute_image
from services.protocol import conversation
from utils.helper import parse_image_count


class SingleImageResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.images = []
        for color in ("red", "blue"):
            buffer = BytesIO()
            Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
            self.images.append(buffer.getvalue())
        self.items = [{"b64_json": base64.b64encode(data).decode("ascii")} for data in self.images]
        self.raw_writer = mock.Mock()
        token = execution_checkpoint.set(ExecutionCheckpoint(persist=lambda **_values: None, raw_writer=self.raw_writer))
        self.addCleanup(execution_checkpoint.reset, token)
        self.save = self.enterContext(mock.patch.object(conversation, "save_image_bytes", return_value="/images/first.png"))
        self.enterContext(mock.patch.object(conversation.config, "get_image_calibration_settings", return_value={"enabled": False}))
        self.backend = mock.Mock()
        self.urls = ["https://files.test/first.png", "https://files.test/second.png"]
        self.backend.resolve_conversation_image_urls.return_value = self.urls
        # Also enforce the limit if a downloader or saved checkpoint contains extras.
        self.backend.download_image_bytes.return_value = self.images
        self.backend._poll_image_results.return_value = (["file-first", "file-second"], [])
        self.request = conversation.ConversationRequest(model="gpt-image-2", prompt="draw")

    def assert_single_result(self, outputs, *, raw_writes=1) -> None:
        result = [output for output in outputs if output.kind == "result"]
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].data), 1)
        self.assertEqual(result[0].data[0]["b64_json"], self.items[0]["b64_json"])
        self.assertEqual(self.raw_writer.call_count, raw_writes)
        for call in self.raw_writer.call_args_list:
            self.assertEqual([item["b64_json"] for item in call.args[0]], [self.items[0]["b64_json"]])
        self.save.assert_called_once_with(self.images[0], None)

    def test_upstream_prompt_requires_one_image_even_without_size_or_quality(self) -> None:
        for size, quality in ((None, ""), ("1536x1024", "high")):
            with self.subTest(size=size, quality=quality):
                prompt = conversation.build_image_prompt("draw two options", size, quality)
                self.assertTrue(prompt.startswith("draw two options"))
                self.assertIn("只生成 1 张图片（n=1）", prompt)
                if size:
                    self.assertIn("宽高比 3:2", prompt)

    def test_count_parser_accepts_only_one_and_rejects_invalid_counts(self) -> None:
        from fastapi import HTTPException
        for value in (None, "", 1, "1"):
            self.assertEqual(parse_image_count(value), 1)
        for value in (0, -1, 2, 16, True, False, 1.5, "2", "1.5", []):
            with self.subTest(value=value), self.assertRaises(HTTPException) as caught:
                parse_image_count(value)
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(caught.exception.detail["error"]["code"], "invalid_image_count")

    def test_limit_precedes_raw_checkpoint_calibration_and_storage(self) -> None:
        for response_format in ("url", "b64_json"):
            with self.subTest(response_format=response_format):
                self.raw_writer.reset_mock()
                self.save.reset_mock()
                with mock.patch.object(conversation, "calibrate_image", wraps=conversation.calibrate_image) as calibrate:
                    result = conversation.format_image_result(
                        [{"b64_json": " "}, *self.items], "draw", response_format, max_images=1,
                    )
                self.assertEqual(len(result["data"]), 1)
                self.assertEqual("b64_json" in result["data"][0], response_format == "b64_json")
                self.raw_writer.assert_called_once_with(self.items[:1])
                self.save.assert_called_once_with(self.images[0], None)
                self.assertEqual(calibrate.call_count, 1)

    def test_all_web_result_paths_keep_one_image(self) -> None:
        for path in ("normal", "text_reply_poll", "empty_reply_poll"):
            with self.subTest(path=path):
                self.raw_writer.reset_mock()
                self.save.reset_mock()
                self.backend.download_image_bytes.reset_mock()
                event = {"type": "conversation.done", "conversation_id": "original", "file_ids": ["file-first"],
                         "text": '{"size":"1024x1024","n":1}' if path == "text_reply_poll" else "",
                         "turn_use_case": "image gen"}
                self.backend.resolve_conversation_image_urls.side_effect = [self.urls] if path == "normal" else [[], self.urls]
                with mock.patch.object(conversation, "conversation_events", return_value=[event]), \
                        mock.patch.object(conversation.time, "sleep"):
                    self.assert_single_result(list(conversation.stream_image_outputs(self.backend, self.request)))
                self.backend.download_image_bytes.assert_called_once_with(self.urls[:1])

    def test_codex_result_keeps_one_image(self) -> None:
        self.backend.iter_codex_image_response_events.return_value = [
            {"type": "image_generation_call", "result": item["b64_json"]} for item in self.items
        ]
        self.assert_single_result(list(conversation.stream_codex_image_outputs(self.backend, self.request)))
        self.assertIn("只生成 1 张图片（n=1）", self.backend.iter_codex_image_response_events.call_args.kwargs["prompt"])

    def test_raw_checkpoint_recovery_keeps_one_image_without_generating(self) -> None:
        with mock.patch.object(conversation.account_service, "acquire_governed_image_token") as acquire:
            self.assert_single_result(execute_image(self.request, 1, 1, {"raw_images": self.items}))
        acquire.assert_not_called()

    def test_conversation_recovery_keeps_one_image_without_resubmitting(self) -> None:
        pool = mock.Mock()
        pool.get_account.return_value = {"pool_account_id": "original-account"}
        row = {"recovery": "recovering_result", "conversation_id": "original", "account_id": "original-account",
               "_account_pool": pool, "_account_token": "synthetic-token"}
        with mock.patch.object(conversation, "OpenAIBackendAPI", return_value=self.backend):
            self.assert_single_result(execute_image(self.request, 1, 1, row), raw_writes=2)
        self.backend.download_image_bytes.assert_called_once_with(self.urls[:1])
        self.backend.stream_conversation.assert_not_called()
        pool.mark_image_result.assert_called_once_with("synthetic-token", True, release_slot=False)

    def test_text_rejection_does_not_create_an_image(self) -> None:
        event = {"type": "conversation.done", "conversation_id": "original", "text": "Unable to generate this image."}
        with mock.patch.object(conversation, "conversation_events", return_value=[event]):
            outputs = list(conversation.stream_image_outputs(self.backend, self.request))
        self.assertEqual([output.kind for output in outputs], ["message"])
        self.backend.download_image_bytes.assert_not_called()
        self.save.assert_not_called()
        self.raw_writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
