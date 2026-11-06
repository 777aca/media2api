"""Native integration tests use synthetic accounts and isolated provider storage."""
import base64
import io
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from services.providers import grok_store
from services.providers.grok_accounts import parse_import, pool
from services.providers.grok_catalog import catalog
from services.providers.grok_protocol import handle, normalize, parse_calls
from services.providers.grok_store import GrokStore
from services.providers.grok_video import VideoRuntime, validate
from services.providers.grok_web import GrokWeb, GrokError, frames


class Response:
    def __init__(self, value=None, status=200, lines=None):
        self.value, self.status_code, self.lines = value, status, lines or []
        self.closed = False
    def json(self): return self.value
    def close(self): self.closed = True
    def iter_lines(self): return iter(self.lines)


class Socket:
    def __init__(self, events): self.events, self.sent, self.closed = iter(events), [], False
    def send_json(self, value): self.sent.append(value)
    def recv(self): return json.dumps(next(self.events)).encode(), 1
    def close(self): self.closed = True


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = GrokStore(Path(self.temp.name))
        self.saved_store, grok_store._store = grok_store._store, self.store
        pool.inflight.clear(); pool.users.clear()
        self.store.import_accounts(parse_import("synthetic-sso"))
        self.account = self.store.accounts()[0]
        self.store.update_account(self.account["id"], {"user_id": str(uuid.uuid4())})
        self.account = self.store.account(self.account["id"], secret=True)
        self.identity = {"id": "user-a", "role": "user", "name": "Synthetic"}

    def tearDown(self):
        self.store.db.close()
        grok_store._store = self.saved_store
        pool.inflight.clear(); pool.users.clear()
        self.temp.cleanup()

    def body(self, **values):
        return {"model": "grok-chat-fast", "messages": [{"role": "user", "content": "hello"}], "_identity": self.identity, **values}

    def test_account_encryption_and_dedup(self):
        self.store.import_accounts(parse_import("sso=synthetic-sso; ignored=1\nsynthetic-sso"))
        self.assertEqual(len(self.store.accounts()), 1)
        row = self.store.db.execute("SELECT secret,metadata FROM grok_accounts").fetchone()
        self.assertNotIn("synthetic-sso", "".join(row))
        self.assertNotIn("sso_token", self.store.accounts()[0])
        self.assertEqual(self.store.account(self.account["id"], secret=True)["sso_token"], "synthetic-sso")

    def test_import_rejects_invalid_and_wrong_provider(self):
        for value in ("[broken", {"provider": "grok_build", "accounts": []}, [{"token": "a\rb"}], [{"token": "x", "tier": "admin"}]):
            with self.subTest(value=value), self.assertRaises(HTTPException): parse_import(value)

    def test_catalog_filters_tier_and_media(self):
        basic = {v["id"] for v in catalog(self.store.accounts())}
        self.assertIn("grok-chat-fast", basic)
        self.assertNotIn("grok-chat-heavy", basic)
        self.store.update_account(self.account["id"], {"tier": "heavy"})
        self.assertIn("grok-chat-heavy", {v["id"] for v in catalog(self.store.accounts())})
        self.assertTrue(all(v["capabilities"][0] in {"image", "image_edit"} for v in catalog(self.store.accounts(), "image,image_edit")))

    def test_lease_releases_on_failure(self):
        with self.assertRaises(RuntimeError):
            with pool.lease("grok-chat-fast", "a"): raise RuntimeError()
        self.assertEqual(sum(pool.inflight.values()), 0)
        self.assertEqual(sum(pool.users.values()), 0)

    def test_account_list_exposes_live_request_count_without_persisting_it(self):
        from api.grok_native import create_router
        route = next(route for route in create_router().routes if route.path == "/api/grok/accounts" and "GET" in route.methods)
        with patch("api.grok_native.require_admin"):
            self.assertEqual(route.endpoint()["items"][0]["request_inflight"], 0)
            with pool.lease("grok-chat-fast", "synthetic-user"):
                item = route.endpoint()["items"][0]
                self.assertEqual(item["request_inflight"], 1)
                self.assertNotIn("sso_token", item)
                self.assertNotIn("request_inflight", self.store.accounts()[0])
            self.assertEqual(route.endpoint()["items"][0]["request_inflight"], 0)

    def test_lease_does_not_use_disabled_or_exhausted(self):
        self.store.update_account(self.account["id"], {"quotas": {"fast": {"remaining": 0, "reset_at": time.time()+30}}})
        with self.assertRaises(HTTPException): pool.acquire("grok-chat-fast")
        self.store.update_account(self.account["id"], {"quotas": {}, "enabled": False})
        with self.assertRaises(HTTPException): pool.acquire("grok-chat-fast")

    def test_concurrency_limits(self):
        self.store.save_settings({"user_concurrency": 1})
        with pool.lease("grok-chat-fast", "a"):
            with self.assertRaises(HTTPException):
                with pool.lease("grok-chat-fast", "a"): pass

    def test_owner_isolation(self):
        self.store.put("response", "r1", "a", {"text": "private"})
        with self.assertRaises(HTTPException): self.store.get("response", "r1", "b")
        with self.assertRaises(HTTPException): self.store.delete("response", "r1", "b")
        self.assertEqual(self.store.list("response", "b"), [])

    def test_settings_secrets_encrypted(self):
        self.store.save_settings({"statsig_value": "synthetic-signature"})
        self.assertEqual(self.store.settings()["statsig_value"], "synthetic-signature")
        self.assertNotIn("synthetic-signature", self.store.get("settings", "grok")["encrypted"])
        with self.assertRaises(HTTPException): self.store.save_settings({"global_concurrency": True})

    def test_gateway_handshake_and_turn(self):
        sid = "conversation-1"
        ws = Socket([{"session_id": sid, "event": {"type": "session.created"}},
                     {"event": {"type": "conversation.attached", "conversation": {"id": sid}}},
                     {"event": {"type": "response.output_text.delta", "delta": "Hi"}},
                     {"event": {"type": "response.done", "response": {"id": "parent", "status": "completed"}}}])
        session = MagicMock(); session.ws_connect.return_value = ws
        with GrokWeb(self.account, session=session) as client:
            events = list(client.chat_events("Hello", "grok-chat-fast"))
        self.assertEqual(events[-1]["conversation_id"], sid)
        self.assertEqual([e["event"]["type"] for e in ws.sent], ["session.create", "conversation.item.create", "response.create"])
        self.assertTrue(ws.closed)
        self.assertIn("/ws/mgw/?uid=", session.ws_connect.call_args.args[0])

    def test_gateway_close_releases_socket(self):
        ws = Socket([{"session_id": "s", "event": {"type": "session.created"}}])
        session = MagicMock(); session.ws_connect.return_value = ws
        with GrokWeb(self.account, session=session) as client:
            stream = client.chat_events("Hello", "grok-chat-fast")
            next(stream); stream.close()
        self.assertTrue(ws.closed)

    def test_gateway_mismatched_conversation_fails(self):
        ws = Socket([{"session_id": "s", "event": {"type": "session.created"}},
                     {"event": {"type": "conversation.attached", "conversation": {"id": "other"}}}])
        session = MagicMock(); session.ws_connect.return_value = ws
        with GrokWeb(self.account, session=session) as client, self.assertRaises(GrokError):
            list(client.chat_events("hi", "grok-chat-fast"))
        self.assertEqual(len(ws.sent), 1)

    def test_http_failure_does_not_leak_upstream_body(self):
        session = MagicMock(); session.request.return_value = Response({"token": "private"}, 401)
        with GrokWeb(self.account, session=session) as client, self.assertRaises(GrokError) as error:
            client.request("GET", "/api/auth/session")
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.store.account(self.account["id"])["status"], "reauth_required")

    def test_asset_download_rejects_foreign_host_before_network(self):
        session = MagicMock()
        with GrokWeb(self.account, session=session) as client:
            for url in ("http://127.0.0.1/a", "https://example.org/a", "https://grok.com.evil.test/a"):
                with self.assertRaises(GrokError): list(client.download(url))
        session.get.assert_not_called()

    def test_chat_representations_and_stored_response(self):
        events = [{"type": "response.output_text.delta", "delta": "hello", "conversation_id": "s"},
                  {"type": "response.done", "response": {"id": "p"}, "conversation_id": "s"}]
        with patch.object(GrokWeb, "chat_events", return_value=iter(events)):
            result = handle(self.body())
        self.assertEqual(result["choices"][0]["message"]["content"], "hello")
        self.assertTrue(result["usage"]["estimated"])
        with patch.object(GrokWeb, "chat_events", return_value=iter(events)):
            result = handle(self.body(input="hello"), "responses")
        self.assertEqual(result["output"][0]["content"][0]["text"], "hello")
        with self.assertRaises(HTTPException): self.store.get("response", result["id"], "other")
        with patch.object(GrokWeb, "chat_events", return_value=iter(events)):
            result = handle(self.body(), "messages")
        self.assertEqual(result["content"][0]["text"], "hello")

    def test_stream_has_final_usage_and_releases_lease(self):
        events = [{"type": "response.output_text.delta", "delta": "Hi"}, {"type": "response.done", "response": {"id": "p"}}]
        with patch.object(GrokWeb, "chat_events", return_value=iter(events)):
            chunks = list(handle(self.body(stream=True)))
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(sum(pool.inflight.values()), 0)

    def test_previous_response_cannot_cross_owner(self):
        self.store.put("response", "r", "other", {"account_id": "x", "expires_at": time.time()+30})
        with self.assertRaises(HTTPException): handle(self.body(input="hi", previous_response_id="r"), "responses")

    def test_tool_arguments_are_validated(self):
        text = '<tool_calls><tool_call><tool_name>weather</tool_name><parameters>{"city":"北京"}</parameters></tool_call></tool_calls>'
        content, calls = parse_calls(text, [{"name": "weather"}])
        self.assertEqual(content, "")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["city"], "北京")
        with self.assertRaises(GrokError): parse_calls(text, [])

    def test_video_validation_rejects_unsupported_modes(self):
        for values in ({"duration": 16}, {"duration": True}, {"image_url": "https://a"}, {"model": "other"}):
            with self.assertRaises(HTTPException): validate({"prompt": "test", **values})

    def test_video_idempotency_and_restart_do_not_replay(self):
        rt = VideoRuntime()
        with patch.object(rt, "_schedule"):
            first = rt.submit(self.identity, {"prompt": "scene"}, "idempotent")
            second = rt.submit(self.identity, {"prompt": "scene"}, "idempotent")
            self.assertEqual(first["id"], second["id"])
            with self.assertRaises(HTTPException): rt.submit(self.identity, {"prompt": "different"}, "idempotent")
        rt.stop()
        job = self.store.get("video", first["id"])
        job["status"] = "in_progress"
        self.store.put("video", job["id"], job["owner"], job)
        rt = VideoRuntime()
        with patch.object(rt, "_schedule") as schedule:
            rt.start(); schedule.assert_not_called()
        self.assertEqual(self.store.get("video", job["id"])["status"], "uncertain")
        rt.stop()

    def test_frames_support_sse_and_ndjson_and_close(self):
        response = Response(lines=[b'event: x', b'data: {"a":1}', b'', b'{"b":2}'])
        self.assertEqual(list(frames(response)), [{"a":1}, {"b":2}])
        self.assertTrue(response.closed)

    def test_routes_use_unified_auth_and_history_isolation(self):
        from api.app import create_app
        from services.auth_service import auth_service
        def auth(token): return {**self.identity, "id": token} if token in {"a", "b"} else None
        with patch.object(auth_service, "authenticate", side_effect=auth), patch("services.model_service.model_catalog_service.list_models", return_value={"data": []}):
            client = TestClient(create_app())
            self.assertEqual(client.get("/api/grok/accounts").status_code, 401)
            self.assertEqual(client.get("/api/grok/accounts", headers={"Authorization": "Bearer a"}).status_code, 403)
            self.assertEqual(client.get("/grok/v1/models", headers={"Authorization": "Bearer a"}).status_code, 200)
            client.put("/api/conversations/same", json={"title":"A"}, headers={"Authorization": "Bearer a"})
            client.put("/api/conversations/same", json={"title":"B"}, headers={"Authorization": "Bearer b"})
            self.assertEqual(client.get("/api/conversations", headers={"Authorization": "Bearer a"}).json()["items"][0]["title"], "A")

    def test_video_content_ticket_and_range(self):
        from api.app import create_app
        from services.auth_service import auth_service
        video_id = "video_" + uuid.uuid4().hex
        self.store.put("video", video_id, "a", {"id": video_id, "status":"completed"})
        folder = self.store.directory / "videos"; folder.mkdir()
        (folder / (video_id + ".mp4")).write_bytes(b"0123456789abcdef")
        with patch.object(auth_service, "authenticate", side_effect=lambda t: {**self.identity,"id":t} if t in {"a","b"} else None):
            client = TestClient(create_app())
            self.assertEqual(client.get(f"/v1/videos/{video_id}/content", headers={"Authorization":"Bearer b"}).status_code,404)
            path = client.post(f"/api/video-tasks/{video_id}/playback", headers={"Authorization":"Bearer a"}).json()["path"]
            result = client.get(path, headers={"Range":"bytes=2-5"})
            self.assertEqual(result.status_code,206)
            self.assertEqual(result.content,b"2345")

    def test_grok_image_uses_existing_quota_once(self):
        from services.generation_runtime import GenerationRuntime
        from services.generation_context import GenerationContext
        from services.protocol.conversation import ConversationRequest
        rt = GenerationRuntime(Path(self.temp.name), identity_resolver=lambda _: self.identity)
        rt.store.set_limits("user-a", {"image_quota_limit": 1, "image_concurrency_limit": 1})
        image = io.BytesIO(); Image.new("RGB", (32,32), "red").save(image, format="PNG")
        request = ConversationRequest(model="grok-imagine-image", prompt="scene")
        context = GenerationContext(self.identity, "/v1/images/generations", "same")
        with patch.object(GrokWeb, "image", return_value=image.getvalue()) as generate, patch("services.protocol.conversation.save_image_bytes", return_value="/synthetic.png"):
            try:
                first = rt.submit(request, context)
                second = rt.submit(request, GenerationContext(self.identity, "/v1/images/generations", "same"))
                self.assertEqual(first, second)
                result = list(rt.outputs(first, request))
                self.assertTrue(any(output.data for output in result))
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(rt.store.quota("user-a")["image_quota_used"], 1)
                self.assertEqual(rt.store.quota("user-a")["image_quota_reserved"], 0)
            finally:
                rt.stop(); rt.store.close()

    def test_imagine_waits_for_completion_and_rejects_moderation(self):
        from services.protocol.conversation import ConversationRequest
        class ImagineSocket(Socket):
            def recv(inner):
                req = inner.sent[-1]["item"]["content"][0]["requestId"]
                event = next(inner.events)
                return json.dumps({"id":"img-1", "requestId":req, **event}).encode(), 1
        for moderated in (False, True):
            ws = ImagineSocket([{"type":"image", "percentage_complete":50,"blob":"cHJldmlldw=="},
                                {"type":"image", "percentage_complete":100,"blob":"ZmluYWw="},
                                {"type":"json", "current_status":"completed", "moderated":moderated}])
            session = MagicMock(); session.ws_connect.return_value = ws
            with GrokWeb(self.account, session=session) as client:
                if moderated:
                    with self.assertRaises(GrokError): client.image(ConversationRequest(model="grok-imagine-image",prompt="test"))
                else:
                    self.assertEqual(client.image(ConversationRequest(model="grok-imagine-image",prompt="test")), b"final")
            self.assertTrue(ws.closed)

    def test_cleanup_preserves_pending_jobs_and_removes_finished_media(self):
        old = time.time()-40*86400
        folder = self.store.directory / "videos"; folder.mkdir()
        for status in ("completed", "queued"):
            key = "video_"+uuid.uuid4().hex
            self.store.put("video",key,"a",{"status":status})
            (folder / (key+".mp4")).write_bytes(b"test")
        self.store.db.execute("UPDATE provider_state SET updated=?",(old,)); self.store.db.commit()
        self.store.cleanup()
        self.assertEqual([j["status"] for j in self.store.list("video")],["queued"])
        self.assertEqual(len(list(folder.glob("*.mp4"))),1)

    def test_tool_round_trip_keeps_call_id_and_arguments(self):
        prompt, *_ = normalize({"input":[{"type":"function_call","call_id":"call_1","name":"weather","arguments":"{}"},
                                        {"type":"function_call_output","call_id":"call_1","output":"sunny"}]},"responses")
        self.assertIn("weather",prompt); self.assertIn("call_1",prompt); self.assertIn("sunny",prompt)
        with self.assertRaises(HTTPException): normalize(self.body(tool_choice={"function":None}),"chat")

    def test_weekly_parser_requires_valid_grpc_period(self):
        import struct
        from services.providers.grok_quota import weekly
        def varint(n):
            out = bytearray()
            while n > 127: out.append((n & 127)|128); n >>= 7
            out.append(n); return bytes(out)
        def field(n,data): return varint((n<<3)|2)+varint(len(data))+data
        start = int(time.time()); end = start+7*86400
        config = b'\x0d'+struct.pack('<f',25.0)+field(4,b'\x08'+varint(start))+field(5,b'\x08'+varint(end))
        message = field(1,config); frame = b'\0'+len(message).to_bytes(4,'big')+message
        self.assertEqual(weekly(frame)["remaining"],7500)
        for invalid in (frame[:-1], b'', b'\0\0\0\0\0'):
            with self.assertRaises(ValueError): weekly(invalid)

    def test_native_http_stream_uses_same_handler_at_legacy_path(self):
        from api.app import create_app
        from services.auth_service import auth_service
        events = [{"type":"response.output_text.delta","delta":"native hello"},{"type":"response.done","response":{"id":"p"}}]
        with patch.object(auth_service,"authenticate",return_value=self.identity), patch.object(GrokWeb,"chat_events",side_effect=lambda *_: iter(events)):
            client = TestClient(create_app())
            for prefix in ("/v1", "/grok/v1"):
                result = client.post(prefix+"/chat/completions",headers={"Authorization":"Bearer synthetic"},json={"model":"grok-chat-fast","messages":[{"role":"user","content":"hi"}],"stream":True})
                self.assertEqual(result.status_code,200)
                self.assertIn("native hello",result.text); self.assertIn("[DONE]",result.text)
            result = client.post("/v1/responses",headers={"Authorization":"Bearer synthetic"},json={"model":"grok-chat-fast","input":"hi"})
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.json()["output"][0]["content"][0]["text"],"native hello")
        self.assertEqual(sum(pool.inflight.values()),0)

    def test_video_worker_persists_result_and_retry_never_resubmits(self):
        rt = VideoRuntime()
        with patch.object(rt,"_schedule"):
            submitted = rt.submit(self.identity,{"prompt":"scene"})
        with patch("services.generation_runtime.GenerationRuntime._resolve_identity",return_value=self.identity), patch.object(GrokWeb,"video",return_value=("https://assets.grok.com/a.mp4",6)) as generate, patch.object(GrokWeb,"download",side_effect=GrokError("download failed")):
            rt._run(submitted["id"])
            self.assertEqual(self.store.get("video",submitted["id"])["status"],"uncertain")
            self.assertEqual(generate.call_count,1)
        with patch.object(rt,"_schedule"), patch("services.generation_runtime.GenerationRuntime._resolve_identity",return_value=self.identity), patch.object(GrokWeb,"video") as generate, patch.object(GrokWeb,"download",return_value=iter([b'\0\0\0\x18ftypisomsynthetic'])):
            rt.retry_download(self.identity["id"],submitted["id"])
            rt._run(submitted["id"])
            generate.assert_not_called()
            self.assertEqual(self.store.get("video",submitted["id"])["status"],"completed")
        rt.stop()

    def test_media_parser_handles_chunked_adjacent_json_and_partial_images(self):
        from services.providers.grok_web import image_urls
        response = Response()
        raw = '{"text":"你好"}{"other":1}'.encode()
        response.iter_content = lambda: (raw[i:i+2] for i in range(0,len(raw),2))
        self.assertEqual(list(frames(response)),[{"text":"你好"},{"other":1}])
        self.assertEqual(list(image_urls({"image_chunk":{"progress":50,"imageUrl":"preview.jpg"}})),[])
        self.assertEqual(list(image_urls({"image_chunk":{"progress":100,"imageUrl":"final.jpg"}})),["final.jpg"])
        with self.assertRaises(GrokError): list(image_urls({"image_chunk":{"moderated":True,"imageUrl":"blocked.jpg"}}))

    def test_native_backup_contains_consistent_database_and_decryption_key(self):
        import tarfile
        from services.backup_service import backup_service
        with patch("services.backup_service.DATA_DIR",self.store.directory):
            raw = backup_service._build_backup_archive({"include":{}},trigger="test")
        with tarfile.open(fileobj=io.BytesIO(raw),mode="r:gz") as archive:
            self.assertIn("data/native-providers.sqlite",archive.getnames())
            self.assertEqual(archive.extractfile("data/.native-provider-key").read(), (self.store.directory/".native-provider-key").read_bytes())


if __name__ == "__main__": unittest.main()
