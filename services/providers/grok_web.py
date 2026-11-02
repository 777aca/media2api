"""Native Grok Web wire adapter, based on grok2api v3.1.8 (MIT).

Never forward a downstream API key to Grok. All upstream sessions use the
selected account's credentials and close when their consuming iterator closes.
"""
from __future__ import annotations

import base64
import codecs
import io
import json
import re
import threading
import time
import uuid
from contextlib import closing
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

from curl_cffi import CurlMime, requests
from fastapi import HTTPException
from PIL import Image

from services.providers.grok_catalog import resolve
from services.providers.grok_store import get_store

ORIGIN = "https://grok.com"
MAX_IMAGE = 32 * 1024 * 1024


class GrokError(Exception):
    def __init__(self, message="Grok 上游请求失败", status_code=502, *, submitted=False):
        super().__init__(message)
        self.status_code = status_code
        self.code = "grok_upstream_error"
        self.submission_uncertain = submitted

    def to_openai_error(self):
        return {"error": {"message": str(self), "type": "upstream_error", "code": self.code}}


def frames(response):
    """Read SSE, NDJSON and adjacent JSON objects without buffering the stream."""
    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    chunks = response.iter_content() if hasattr(response, "iter_content") else (line + b"\n" if isinstance(line, bytes) else line + "\n" for line in response.iter_lines())
    try:
        for chunk in chunks:
            buffer += utf8.decode(chunk) if isinstance(chunk, bytes) else chunk
            if len(buffer) > 16 * 1024 * 1024:
                raise GrokError("Grok 响应帧过大", submitted=True)
            while buffer:
                buffer = buffer.lstrip()
                if buffer.startswith(("event:", ":", "id:", "retry:")):
                    if "\n" not in buffer:
                        break
                    buffer = buffer.split("\n", 1)[1]
                    continue
                if buffer.startswith("data:"):
                    buffer = buffer[5:].lstrip()
                if buffer.startswith("[DONE]"):
                    buffer = buffer[6:]
                    continue
                if not buffer:
                    break
                try:
                    value, end = decoder.raw_decode(buffer)
                except ValueError:
                    break
                buffer = buffer[end:]
                if isinstance(value, dict):
                    if value.get("error") or value.get("result", {}).get("response", {}).get("error"):
                        raise GrokError("Grok 上游拒绝了生成请求", submitted=True)
                    yield value
        if buffer.strip() or utf8.decode(b"", final=True):
            raise GrokError("Grok 媒体响应不完整", submitted=True)
    except GrokError:
        raise
    except Exception as exc:
        raise GrokError("Grok 媒体响应中断或无效", submitted=True) from exc
    finally:
        response.close()


def image_urls(value):
    if isinstance(value, dict):
        if value.get("moderated"):
            raise GrokError("Grok 拒绝了图片内容", 400, submitted=True)
        if ("progress" in value and value["progress"] < 100) or value.get("isFinal") is False:
            return
        for key, item in value.items():
            if key in {"imageUrl", "image_url", "imageURL", "generatedImageUrl", "fileUri", "url"} and isinstance(item, str) and (key != "url" or value.get("progress", 0) >= 100 or value.get("isFinal")):
                yield item
            elif key in {"generatedImageUrls", "imageUrls", "fileAttachments"} and isinstance(item, list):
                yield from (x for x in item if isinstance(x, str))
            else:
                yield from image_urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from image_urls(item)
    elif isinstance(value, str) and value.startswith("{"):
        try:
            yield from image_urls(json.loads(value))
        except ValueError:
            pass


class _MetaParser(HTMLParser):
    value = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name") == "grok-site-verification":
            self.value = a.get("content", "")


class GrokWeb:
    def __init__(self, account: dict, *, session=None, origin=ORIGIN):
        self.account = account
        self.settings = get_store().settings()
        self.origin = origin.rstrip("/")
        self.submitted = False
        self.session = session
        if self.session is None:
            from services.proxy_service import proxy_settings
            kwargs = proxy_settings.build_session_kwargs(proxy=self.settings["proxy_url"], upstream=True)
            self.session = requests.Session(impersonate="chrome", **kwargs)

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def headers(self):
        token = self.account["sso_token"]
        cookie = f"sso={token}; sso-rw={token}"
        if self.account.get("cloudflare_cookies"):
            cookie += "; " + self.account["cloudflare_cookies"]
        return {"Cookie": cookie, "Origin": self.origin, "Referer": self.origin + "/",
                "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache", "x-xai-request-id": str(uuid.uuid4())}

    def check(self, response):
        if not 200 <= response.status_code < 300:
            status = response.status_code
            response.close()
            if status == 401:
                get_store().update_account(self.account["id"], {"status": "reauth_required"})
            elif status == 429:
                get_store().update_account(self.account["id"], {"cooldown_until": time.time() + 60})
            raise GrokError(f"Grok 上游返回 HTTP {status}", status if status in {400, 401, 403, 429} else 502)
        return response

    def request(self, method, path, *, signed=False, **kwargs):
        headers = self.headers()
        headers.update(kwargs.pop("headers", {}))
        if signed:
            headers["x-statsig-id"] = self.sign(method, path)
        try:
            response = self.session.request(method, self.origin + path, headers=headers,
                                            allow_redirects=False, timeout=kwargs.pop("timeout", 30), **kwargs)
        except Exception as exc:
            raise GrokError("Grok 网络连接失败或超时", submitted=self.submitted) from exc
        return self.check(response)

    def sign(self, method, path):
        try:
            return self._sign(method, path)
        except GrokError:
            raise
        except Exception as exc:
            raise GrokError("Grok 签名服务连接失败或响应无效", 503) from exc

    def _sign(self, method, path):
        if self.settings["statsig_value"]:
            return self.settings["statsig_value"]
        endpoint = self.settings["signer_url"]
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise GrokError("请配置有效的 HTTPS 签名服务或手动 Statsig 值", 503)
        # Only public page metadata is sent to the signer, never account cookies.
        page = self.session.get(self.origin + "/index", headers=self.headers(), timeout=15, allow_redirects=False)
        try:
            if page.status_code == 404:
                page.close()
                page = self.request("GET", "/")
            else:
                self.check(page)
            parser = _MetaParser()
            parser.feed(page.text[:4 * 1024 * 1024])
        finally:
            page.close()
        if not parser.value:
            raise GrokError("Grok 页面缺少签名元数据")
        with requests.Session() as signer:
            response = signer.post(endpoint, json={"method": method.upper(), "path": path,
                "environment": {"metaContent": parser.value}}, timeout=12, allow_redirects=False)
            if response.status_code != 200 or len(response.content) > 4096:
                raise GrokError("Grok 签名服务不可用", 503)
            result = response.json().get("x-statsig-id", "")
            if not isinstance(result, str) or not result or "\n" in result or "\r" in result:
                raise GrokError("Grok 签名服务响应无效", 503)
            return result

    def identity(self):
        with closing(self.request("GET", "/api/auth/session")) as r:
            value = r.json()
        if str(value.get("status", "")).lower() in {"unauthenticated", "blocked"}:
            raise GrokError("Grok 账号需要重新登录", 401)
        candidates = [value.get("session", {}), value.get("user", {}), value]
        uid = next((v.get("userId") or v.get("id") or v.get("sub") for v in candidates if v.get("userId") or v.get("id") or v.get("sub")), "")
        try:
            uid = str(uuid.UUID(uid))
        except (ValueError, TypeError) as exc:
            raise GrokError("Grok 未返回有效账号标识") from exc
        values = {"user_id": uid, "email": next((v["email"] for v in candidates if v.get("email")), "")}
        get_store().update_account(self.account["id"], values)
        self.account.update(values)
        return values

    def sync(self):
        identity = self.identity()
        quotas = {}
        for mode in ("fast", "auto"):
            with closing(self.request("POST", "/rest/rate-limits", signed=True, json={"modelName": mode})) as r:
                q = r.json()
            if not isinstance(q.get("totalQueries"), int) or q["totalQueries"] <= 0:
                raise GrokError("Grok 额度响应不完整，保留原快照")
            quotas[mode] = {"remaining": max(0, q.get("remainingQueries", 0)), "total": q["totalQueries"],
                            "reset_at": time.time() + max(1, q.get("windowSizeSeconds", 7200))}
        with closing(self.request("POST", "/rest/media/imagine/quota_info", signed=True, json={})) as r:
            media = r.json()
        for field, mode in (("imagePro", "image_pro"), ("imageEdit", "image_edit"), ("video", "video"), ("video720p", "video720p")):
            if field not in media:
                raise GrokError("Grok 媒体额度响应不完整，保留原快照")
            q = media[field]
            if q is None or (q.get("available") and "remainingQueries" not in q):
                continue
            quotas[mode] = {"remaining": max(0, q.get("remainingQueries", 0)) if q.get("available") else 0,
                           "reset_at": time.time() + max(1, q.get("windowSizeSeconds", 86400))}
        levels = {"fast": {30: 1, 140: 2, 400: 3}, "auto": {7: 1, 20: 1, 50: 2, 150: 3}}
        detected = [levels[mode][quotas[mode]["total"]] for mode in levels if quotas[mode]["total"] in levels[mode]]
        tier = ("basic", "super", "heavy")[min(detected)-1] if detected else self.account.get("tier", "auto")
        if tier in {"super", "heavy"}:
            from services.providers.grok_quota import weekly
            with closing(self.request("POST", "/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig", data=b"\0\0\0\0\0",
                         headers={"Content-Type": "application/grpc-web+proto", "x-grpc-web": "1", "x-user-agent": "connect-es/2.1.1"})) as r:
                try:
                    quotas["weekly"] = weekly(r.content)
                except ValueError as exc:
                    raise GrokError("Grok 周额度响应不完整，保留原快照") from exc
        return get_store().update_account(self.account["id"], {**identity, "quotas": quotas,
            "tier": tier, "last_synced": time.time(), "status": "active", "cooldown_until": 0})

    def upload(self, data_uri, *, edit=False):
        if not isinstance(data_uri, str):
            raise HTTPException(400, "图片输入必须是 data URL")
        encoded = data_uri.partition(",")[2] if data_uri.startswith("data:") else data_uri
        try:
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > MAX_IMAGE:
                raise ValueError()
            with Image.open(io.BytesIO(raw)) as img:
                mime = Image.MIME.get(img.format)
                img.verify()
            if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ValueError()
        except Exception as exc:
            raise HTTPException(400, "图片必须是有效的 PNG/JPEG/WebP/GIF，最大 32 MiB") from exc
        with closing(CurlMime()) as multipart:
            multipart.addpart(name="file", content_type=mime, filename="image." + mime.split("/")[1], data=raw)
            if edit:
                multipart.addpart(name="file_source", data="IMAGINE_SELF_UPLOAD_FILE_SOURCE")
            with closing(self.request("POST", "/http/upload-file-v2/direct", multipart=multipart, timeout=60)) as r:
                value = r.json()
        if value.get("terminalError"):
            raise GrokError("Grok 拒绝了上传图片", 400)
        metadata = value.get("fileMetadata") or {}
        file_id = metadata.get("fileMetadataId") or (None if edit else metadata.get("fileId") or value.get("uploadId"))
        if not file_id:
            raise GrokError("Grok 上传未返回文件标识")
        return file_id

    def socket(self, path):
        try:
            return self.session.ws_connect(self.origin.replace("https:", "wss:").replace("http:", "ws:") + path,
                                            headers=self.headers(), timeout=30)
        except Exception as exc:
            raise GrokError("Grok WebSocket 连接失败") from exc

    def chat_events(self, prompt, model, attachments=(), previous=None):
        spec = resolve(model)
        uid = self.account.get("user_id") or self.identity()["user_id"]
        files = [self.upload(item) for item in attachments]
        ws = self.socket("/ws/mgw/?" + urlencode({"uid": uid}))
        done = threading.Event()
        timer = threading.Timer(self.settings["chat_timeout"], ws.close)
        timer.daemon = True
        lock = threading.Lock()
        def send(event):
            with lock:
                ws.send_json(event)
        def heartbeat():
            while not done.wait(25):
                try:
                    send({"event": {"type": "ping", "event_id": "evt_" + uuid.uuid4().hex}})
                except Exception:
                    return
        timer.start()
        threading.Thread(target=heartbeat, daemon=True).start()
        sid = (previous or {}).get("conversation_id", "")
        flags = {"protocol_capabilities": ["conversation_attached", "custom_methods_v1"], "use_chunk": True,
                 "enable_side_by_side": True, "force_side_by_side": False, "enable_image_generation": spec.capability == "image",
                 "image_generation_count": 2, "disable_artifact": True, "disable_text_follow_ups": False}
        flags.update({"conversation_id": sid, "load_existing": True, "needs_history": False} if previous else
                     {"keep_context": False, "is_temporary": True, "disable_memory": True})
        event = {"event": {"type": "session.create", "event_id": "evt_init_" + uuid.uuid4().hex,
                            "session": {"model": spec.mode, "x_grok": flags}}}
        if sid:
            event["session_id"] = sid
        created = attached = sent = False
        try:
            send(event)
            while True:
                raw, _ = ws.recv()
                if len(raw) > 16 * 1024 * 1024:
                    raise GrokError("Grok 响应帧过大", submitted=sent)
                value = json.loads(raw)
                event = value.get("event") or {}
                kind = event.get("type")
                if kind == "session.created":
                    created = True
                    sid = sid or value.get("session_id", "")
                elif kind == "conversation.attached":
                    cid = event.get("conversation", {}).get("id", "")
                    if not cid or (sid and cid != sid):
                        raise GrokError("Grok 返回了不一致的会话标识")
                    attached, sid = True, cid
                if created and attached and not sent:
                    chunks = [{"mention": {"target": {"file_mention": {"file_id": f}}}} for f in files]
                    chunks.append({"text": {"text": prompt}})
                    item = {"type": "message", "role": "user", "x_grok": {"client_message_id": str(uuid.uuid4()), "input_chunks": chunks}}
                    e = {"type": "conversation.item.create", "event_id": "evt_" + uuid.uuid4().hex, "item": item}
                    if files:
                        item["file_attachment_ids"] = e["file_attachment_ids"] = files
                    if previous:
                        e["parent_response_id"] = previous["parent_id"]
                    self.submitted = sent = True
                    send({"session_id": sid, "event": e})
                    send({"session_id": sid, "event": {"type": "response.create", "event_id": "evt_" + uuid.uuid4().hex}})
                if kind in {"error", "session.ended"}:
                    raise GrokError("Grok 会话异常结束", submitted=sent)
                yield {**event, "conversation_id": sid}
                if kind == "response.done":
                    if event.get("response", {}).get("status", "completed") != "completed":
                        raise GrokError("Grok 响应未完成", submitted=True)
                    return
        except (GrokError, HTTPException):
            raise
        except Exception as exc:
            raise GrokError("Grok 对话连接中断或超时", submitted=sent) from exc
        finally:
            done.set()
            timer.cancel()
            ws.close()

    def download(self, url, *, limit=MAX_IMAGE):
        if not urlsplit(url).scheme and not url.startswith("//"):
            url = "https://assets.grok.com/" + url.lstrip("/")
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname not in {"assets.grok.com", "imgen.x.ai", "grok.com"} or p.username or p.port not in (None, 443):
            raise GrokError("Grok 返回了不可信的媒体地址")
        try:
            response = self.session.get(url, headers=self.headers(), stream=True, timeout=120, allow_redirects=False)
        except Exception as exc:
            raise GrokError("Grok 媒体下载连接失败", submitted=self.submitted) from exc
        self.check(response)
        try:
            count = 0
            for chunk in response.iter_content():
                count += len(chunk)
                if count > limit:
                    raise GrokError("Grok 媒体超过大小上限")
                yield chunk
        except GrokError:
            raise
        except Exception as exc:
            raise GrokError("Grok 媒体下载中断", submitted=self.submitted) from exc
        finally:
            response.close()

    def image(self, request):
        spec = resolve(request.model)
        ratio = {"1024x1024": "1:1", "1536x1024": "3:2", "1024x1536": "2:3",
                 "1792x1024": "3:2", "1024x1792": "2:3", "1280x720": "16:9", "720x1280": "9:16"}.get(request.size, "auto")
        from services.generation_context import checkpoint
        if request.images:
            if spec.capability != "image_edit":
                raise HTTPException(400, "图片编辑请选择 grok-imagine-image-edit")
            assets = [self.upload(i, edit=True) for i in request.images]
            payload = {"modelName": "imagine-image-edit", "message": request.prompt, "enableImageStreaming": True,
                       "enableSideBySide": True, "sendFinalMetadata": True,
                       "mediaGenInput": {"imageToImage": {"prompt": request.prompt, "inputAssets": assets, "aspectRatio": ratio}}}
            checkpoint("submitting")
            self.submitted = True
            r = self.request("POST", "/rest/app-chat/conversations/new", signed=True, json=payload,
                             stream=True, timeout=self.settings["image_timeout"])
            for event in frames(r):
                urls = list(image_urls(event))
                if urls:
                    return b"".join(self.download(urls[0]))
            raise GrokError("Grok 没有返回编辑图片", submitted=True)
        if spec.id.endswith("-lite"):
            checkpoint("submitting")
            for event in self.chat_events("Drawing: " + request.prompt, spec.id):
                urls = list(image_urls(event))
                if urls:
                    return b"".join(self.download(urls[0]))
            raise GrokError("Grok Lite 没有返回图片", submitted=True)
        if spec.capability != "image":
            raise HTTPException(400, "请选择 Grok 图片生成模型")
        ws = self.socket("/ws/imagine/listen")
        timer = threading.Timer(self.settings["image_timeout"], ws.close)
        timer.daemon = True
        timer.start()
        req_id = "img_" + uuid.uuid4().hex
        slots = {}
        try:
            ws.send_json({"type": "conversation.item.create", "timestamp": int(time.time()*1000),
                          "item": {"type": "message", "content": [{"type": "reset"}]}})
            checkpoint("submitting")
            self.submitted = True
            ws.send_json({"type": "conversation.item.create", "timestamp": int(time.time()*1000), "item": {
                "type": "message", "content": [{"requestId": req_id, "text": request.prompt, "type": "input_text", "properties": {
                    "section_count": 0, "is_kids_mode": False, "enable_nsfw": False, "skip_upsampler": False,
                    "enable_side_by_side": True, "is_initial": False, "aspect_ratio": ratio,
                    "enable_pro": spec.id.endswith("2.0"), "num_generations": 1}}]}})
            while True:
                raw, _ = ws.recv()
                if len(raw) > 64 * 1024 * 1024:
                    raise GrokError("Grok 图片帧过大", submitted=True)
                event = json.loads(raw)
                if event.get("type") == "error":
                    raise GrokError("Grok 拒绝了图片请求", submitted=True)
                request_id = next((event.get(k) for k in ("requestId", "request_id", "req_id", "reqId") if event.get(k)), None)
                request_id = request_id or event.get("item", {}).get("requestId") or event.get("item", {}).get("request_id")
                if request_id != req_id:
                    continue
                image_id = event.get("image_id") or event.get("job_id") or event.get("id") or event.get("url")
                if not image_id:
                    continue
                slot = slots.setdefault(image_id, {})
                if event.get("type") == "image" and event.get("percentage_complete", 100) >= 100:
                    slot.update(blob=event.get("blob"), url=event.get("url"))
                if event.get("type") == "json" and event.get("current_status") == "completed":
                    if event.get("moderated"):
                        raise GrokError("Grok 拒绝了图片内容", 400, submitted=True)
                    slot["completed"] = True
                    if event.get("url") and not slot.get("blob"):
                        slot.update(url=event["url"], blob=event.get("blob"))
                if slot.get("completed"):
                    if slot.get("blob"):
                        raw = base64.b64decode(slot["blob"].split(",", 1)[-1], validate=True)
                        if len(raw) > MAX_IMAGE:
                            raise GrokError("Grok 图片超过大小上限", submitted=True)
                        return raw
                    if slot.get("url"):
                        return b"".join(self.download(slot["url"]))
        except (GrokError, HTTPException):
            raise
        except Exception as exc:
            raise GrokError("Grok 图片连接中断或超时", submitted=self.submitted) from exc
        finally:
            timer.cancel()
            ws.close()

    def video(self, body, progress):
        duration = body["duration"]
        if self.account.get("tier") in {"auto", "basic"}:
            duration = min(duration, self.settings["free_video_duration_cap"])
            if body["resolution"] != "720p":
                raise HTTPException(400, "Basic 账号仅支持 720p 视频")
        payload = {"modelName": "imagine-video-gen", "message": body["prompt"] + " --mode=custom",
                   "enableImageStreaming": True, "enableSideBySide": True, "sendFinalMetadata": True,
                   "responseMetadata": {"experiments": [], "modelConfigOverride": {"modelMap": {}}},
                   "mediaGenInput": {"textToVideo": {"prompt": body["prompt"], "aspectRatio": body["aspect_ratio"],
                    "duration": duration, "resolutionName": body["resolution"]}}, "kind": "CONVERSATION_KIND_IMAGINE"}
        self.submitted = True
        response = self.request("POST", "/rest/app-chat/conversations/new", signed=True, json=payload,
                                stream=True, timeout=self.settings["video_timeout"])
        for value in frames(response):
            event = value.get("result", {}).get("response", {}).get("streamingVideoGenerationResponse", {})
            progress(max(0, min(99, int(event.get("progress", 0)))))
            if event.get("moderated"):
                raise GrokError("Grok 拒绝了视频内容", 400)
            url = next((event[k] for k in ("videoUrl", "contentUrl", "contentURL", "assetUrl", "assetURL", "fileUri", "fileURL") if event.get(k)), "")
            if url:
                return url, duration
            for url in value.get("result", {}).get("response", {}).get("modelResponse", {}).get("fileAttachments", []):
                if isinstance(url, str) and ".mp4" in url:
                    return url, duration
        raise GrokError("Grok 视频响应结束但没有结果", submitted=True)
