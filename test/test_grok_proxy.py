from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import grok
from api.errors import install_exception_handlers


class GrokProxyTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict("os.environ", {"MEDIA2API_GROK_GATEWAY_URL": "http://grok-test:8000"})
        self.env.start()
        self.addCleanup(self.env.stop)
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(grok.create_router())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_disabled_and_unknown_paths_do_not_contact_gateway(self):
        with mock.patch.dict("os.environ", {"MEDIA2API_GROK_GATEWAY_URL": ""}):
            self.assertEqual(self.client.get("/grok/v1/models").status_code, 503)
        self.assertEqual(self.client.get("/grok/v1/admin/accounts").status_code, 404)
        self.assertEqual(self.client.post("/grok/v1/videos/example").status_code, 404)

    def test_forwards_stream_and_client_key_without_cookies_or_forwarded_headers(self):
        seen = []

        class EventStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: test\n\n"
                yield b"data: [DONE]\n\n"

        async def upstream(request):
            seen.append((request, await request.aread()))
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=EventStream())

        real_client = httpx.AsyncClient
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(upstream), **kwargs)

        with mock.patch.object(grok.httpx, "AsyncClient", side_effect=client_factory):
            response = self.client.post(
                "/grok/v1/chat/completions?test=1",
                headers={"Authorization": "Bearer grok-test-key", "Cookie": "private=1", "X-Forwarded-For": "bad"},
                json={"model": "grok-chat-fast", "messages": [], "stream": True},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"data: test\n\ndata: [DONE]\n\n")
        request, body = seen[0]
        self.assertEqual(str(request.url), "http://grok-test:8000/v1/chat/completions?test=1")
        self.assertEqual(request.headers["authorization"], "Bearer grok-test-key")
        self.assertNotIn("cookie", request.headers)
        self.assertNotIn("x-forwarded-for", request.headers)
        self.assertEqual(json.loads(body)["model"], "grok-chat-fast")

    def test_image_count_is_checked_for_json_and_multipart_before_forwarding(self):
        for path, options in (
            ("generations", {"json": {"model": "grok-imagine-image", "prompt": "test", "n": 2}}),
            ("edits", {"data": {"model": "grok-imagine-image-edit", "prompt": "test", "n": "2"},
                       "files": {"image": ("test.png", b"synthetic", "image/png")}}),
        ):
            with self.subTest(path=path):
                result = self.client.post(f"/grok/v1/images/{path}", **options)
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.json()["error"]["code"], "invalid_image_count")

    def test_upstream_connection_error_does_not_reveal_url_or_credentials(self):
        real_client = httpx.AsyncClient
        async def failure(request):
            raise httpx.ConnectError("private-token", request=request)
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(failure), **kwargs)
        with mock.patch.object(grok.httpx, "AsyncClient", side_effect=client_factory):
            result = self.client.get("/grok/v1/models", headers={"Authorization": "Bearer private-token"})
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("private-token", result.text)

    def test_video_upload_ticket_is_forwarded_but_redacted_from_access_scope(self):
        seen = []
        scopes = []

        class EmptyStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                if False:
                    yield b""

        async def upstream(request):
            seen.append((str(request.url), await request.aread()))
            return httpx.Response(204, stream=EmptyStream())

        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(grok.create_router())

        async def capture_scope(scope, receive, send):
            await app(scope, receive, send)
            scopes.append((scope["path"], scope["query_string"]))

        real_client = httpx.AsyncClient
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(upstream), **kwargs)

        ticket = "a" * 64
        with TestClient(capture_scope) as client, mock.patch.object(grok.httpx, "AsyncClient", side_effect=client_factory):
            response = client.put(f"/grok/v1/media/uploads/{ticket}?sensitive=test", content=b"video")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(seen, [(f"http://grok-test:8000/v1/media/uploads/{ticket}?sensitive=test", b"video")])
        self.assertEqual(scopes, [("/grok/v1/media/uploads/[REDACTED]", b"")])


if __name__ == "__main__":
    unittest.main()
