import base64
import hashlib
import io
import json
import unittest
from unittest import mock

from PIL import Image, PngImagePlugin
from starlette.datastructures import Headers, UploadFile

from services import request_log


class RequestAssetLoggingTests(unittest.TestCase):
    def setUp(self):
        buffer = io.BytesIO()
        Image.new("RGB", (7, 5), "red").save(buffer, format="PNG")
        self.payload = buffer.getvalue()
        self.encoded = base64.b64encode(self.payload).decode("ascii")

    def test_equivalent_sources_have_same_fingerprint_and_image_metadata(self):
        params = {"base64_images": [self.encoded],
                  "image_url": "data:image/png;base64," + self.encoded,
                  "buffer": self.payload,
                  "upload": (self.payload, "sample.png", "image/png")}
        with mock.patch.object(PngImagePlugin.PngImageFile, "load", side_effect=AssertionError("pixels loaded")):
            result = request_log.sanitize_request_parameters(params)
        for item in [result["base64_images"][0], result["image_url"], result["buffer"], result["upload"]]:
            self.assertIsInstance(item, dict)
            self.assertEqual(item.get("sha256"), hashlib.sha256(self.payload).hexdigest())
            self.assertEqual(item.get("size_bytes"), len(self.payload))
            self.assertEqual(item.get("mime_type"), "image/png")
            self.assertEqual((item.get("width"), item.get("height")), (7, 5))
            self.assertEqual(item.get("decode_status"), "valid")
            self.assertTrue(item["content_omitted"])
        self.assertEqual(result["base64_images"][0]["length"], len(self.encoded))
        self.assertEqual(result["upload"]["type"], "file")
        self.assertNotIn(self.encoded, json.dumps(result))
        self.assertEqual(request_log.sanitize_request_parameters(result), result)

    def test_invalid_data_is_omitted_without_invented_facts(self):
        for value in ["private!invalid", "éééé", "a===", "data:image/png;base64,private!invalid",
                      "data:image/png;base64", "data:text/plain,%ZZ"]:
            with self.subTest(value=value):
                result = request_log.sanitize_request_parameters({"base64": value})["base64"]
                self.assertEqual(result.get("decode_status"), "invalid")
                for field in ("sha256", "size_bytes", "width", "height"):
                    self.assertNotIn(field, result)
                self.assertNotIn(value, json.dumps(result))

    def test_small_png_in_image_fields_is_summarized_without_raw_content(self):
        self.assertLess(len(self.encoded), 256)
        for field in ("images", "image", "mask"):
            with self.subTest(field=field):
                value = [self.encoded] if field == "images" else self.encoded
                result = request_log.sanitize_request_parameters({field: value})
                item = result[field][0] if field == "images" else result[field]
                self.assertIsInstance(item, dict)
                self.assertEqual(item["type"], "base64")
                self.assertTrue(item["content_omitted"])
                self.assertEqual(item["length"], len(self.encoded))
                self.assertEqual(item["size_bytes"], len(self.payload))
                self.assertEqual(item["sha256"], hashlib.sha256(self.payload).hexdigest())
                self.assertEqual(item["mime_type"], "image/png")
                self.assertEqual((item["width"], item["height"]), (7, 5))
                self.assertEqual(item["decode_status"], "valid")
                self.assertNotIn(self.encoded, json.dumps(result))

    def test_image_field_urls_are_preserved_without_reading_or_decoding(self):
        urls = ["http://example.test/image.png", "https://example.test/image.png",
                "file:///C:/synthetic/image.png", "file://synthetic/image.png"]
        for field in ("images", "image", "mask"):
            for url in urls:
                with self.subTest(field=field, url=url):
                    value = [url] if field == "images" else url
                    with mock.patch("builtins.open", side_effect=AssertionError("file read")), \
                         mock.patch("urllib.request.urlopen", side_effect=AssertionError("URL read")), \
                         mock.patch.object(request_log.base64, "b64decode", side_effect=AssertionError("URL decoded")):
                        result = request_log.sanitize_request_parameters({field: value})
                    self.assertEqual(result, {field: value})
        result = request_log.sanitize_request_parameters({"image": "https://example.test/a.png?token=private"})
        self.assertIsInstance(result["image"], str)
        self.assertNotIn("private", result["image"])

    def test_non_image_bytes_and_percent_encoded_data_uri(self):
        result = request_log.sanitize_request_parameters({"image": "data:text/plain,hello%20world",
                                                          "buffer": b"hello world"})
        self.assertIsInstance(result["image"], dict)
        self.assertEqual(result["image"].get("sha256"), hashlib.sha256(b"hello world").hexdigest())
        self.assertEqual(result["buffer"].get("sha256"), result["image"]["sha256"])
        self.assertNotIn("width", result["image"])
        self.assertNotIn("mime_type", result["buffer"])

    def test_decode_limit_and_shared_budget_skip_expensive_work(self):
        with mock.patch.object(request_log, "MAX_ASSET_BYTES", len(self.payload), create=True):
            result = request_log.sanitize_request_parameters({"base64_images": [self.encoded, self.encoded]})
        self.assertEqual(result["base64_images"][0].get("decode_status"), "valid")
        self.assertEqual(result["base64_images"][1].get("decode_status"), "omitted_too_large")
        for value in (self.encoded, "data:image/png;base64," + self.encoded, self.payload):
            with self.subTest(kind=type(value)), mock.patch.object(request_log, "MAX_ASSET_BYTES", 8, create=True):
                with mock.patch("base64.b64decode", side_effect=AssertionError("decoded oversized data")):
                    item = request_log.sanitize_request_parameters({"base64": value})["base64"]
                self.assertEqual(item.get("decode_status"), "omitted_too_large")
                self.assertNotIn("sha256", item)
                self.assertNotIn("width", item)

    def test_upload_file_uses_only_existing_metadata(self):
        stream = mock.Mock()
        stream.read.side_effect = stream.seek.side_effect = AssertionError("stream consumed")
        upload = UploadFile(stream, filename="sample.png", size=42,
                            headers=Headers({"content-type": "image/png"}))
        item = request_log.sanitize_request_parameters({"upload": upload})["upload"]
        self.assertEqual(item.get("mime_type"), "image/png")
        self.assertEqual(item["size_bytes"], 42)
        for field in ("sha256", "width", "height", "decode_status"):
            self.assertNotIn(field, item)
        self.assertEqual(stream.mock_calls, [])

    def test_byte_views_use_byte_count_and_unknown_upload_size_is_absent(self):
        raw = b"abcdefgh"
        for value in (bytearray(raw), memoryview(raw).cast("I"), memoryview(raw)[::2]):
            with self.subTest(kind=type(value)):
                item = request_log.sanitize_request_parameters({"buffer": value})["buffer"]
                self.assertEqual(item["size_bytes"], len(bytes(value)))
                self.assertEqual(item["sha256"], hashlib.sha256(bytes(value)).hexdigest())
        upload = UploadFile(mock.Mock(), filename="unknown")
        item = request_log.sanitize_request_parameters({"upload": upload})["upload"]
        self.assertNotIn("size_bytes", item)
        self.assertNotIn("mime_type", item)

    def test_percent_data_is_size_checked_before_decoding(self):
        with mock.patch.object(request_log, "MAX_ASSET_BYTES", 4):
            with mock.patch.object(request_log, "unquote_to_bytes", side_effect=AssertionError("oversized decode")):
                item = request_log.sanitize_request_parameters({"image": "data:text/plain,12345"})["image"]
        self.assertEqual(item.get("decode_status"), "omitted_too_large")
        self.assertNotIn("sha256", item)

    def test_url_redaction_and_secret_precedence_are_preserved(self):
        result = request_log.sanitize_request_parameters({"image_url": "https://example.test/a.png?token=private",
                                                          "api_key": self.payload})
        self.assertNotIn("private", result["image_url"])
        self.assertEqual(result["api_key"], request_log.REDACTED)


if __name__ == "__main__":
    unittest.main()
