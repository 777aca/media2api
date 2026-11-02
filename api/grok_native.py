"""Native Grok management and media routes, sharing media2api authentication."""
import json
import re
import time
from urllib.parse import quote

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import FileResponse

from api.support import require_admin, require_identity
from services.providers.grok_accounts import parse_import
from services.providers.grok_catalog import catalog
from services.providers.grok_store import get_store
from services.providers.grok_video import public, runtime
from services.providers.grok_web import GrokWeb


def create_router():
    router = APIRouter()

    @router.get("/api/grok/accounts")
    def accounts(authorization: str | None = Header(None)):
        require_admin(authorization)
        return {"items": get_store().accounts()}

    @router.post("/api/grok/accounts/import")
    def import_accounts(body: dict = Body(...), authorization: str | None = Header(None)):
        require_admin(authorization)
        return {"imported": get_store().import_accounts(parse_import(body.get("data", "")))}

    @router.patch("/api/grok/accounts/{account_id}")
    def update_account(account_id: str, body: dict = Body(...), authorization: str | None = Header(None)):
        require_admin(authorization)
        if any(k not in {"enabled", "name", "tier"} for k in body):
            raise HTTPException(400, "不支持的账号字段")
        if "enabled" in body and type(body["enabled"]) is not bool:
            raise HTTPException(400, "enabled 必须是布尔值")
        if "tier" in body and body["tier"] not in {"auto", "basic", "super", "heavy"}:
            raise HTTPException(400, "无效账号等级")
        if "name" in body and (not isinstance(body["name"], str) or len(body["name"]) > 255):
            raise HTTPException(400, "账号名称过长或无效")
        return get_store().update_account(account_id, body)

    @router.delete("/api/grok/accounts/{account_id}")
    def delete_account(account_id: str, authorization: str | None = Header(None)):
        require_admin(authorization)
        get_store().delete_account(account_id)
        return {"deleted": True}

    @router.post("/api/grok/accounts/{account_id}/sync")
    def sync_account(account_id: str, authorization: str | None = Header(None)):
        require_admin(authorization)
        with GrokWeb(get_store().account(account_id, secret=True)) as client:
            return client.sync()

    @router.get("/api/grok/settings")
    def settings(authorization: str | None = Header(None)):
        require_admin(authorization)
        value = get_store().settings()
        return {**value, "proxy_url": "", "statsig_value": "", "has_proxy": bool(value["proxy_url"]), "has_statsig": bool(value["statsig_value"])}

    @router.patch("/api/grok/settings")
    def save_settings(body: dict = Body(...), authorization: str | None = Header(None)):
        require_admin(authorization)
        get_store().save_settings(body)
        return {"saved": True}

    @router.get("/api/video-models")
    def models(authorization: str | None = Header(None)):
        require_identity(authorization)
        return {"data": catalog(get_store().accounts(), "video")}

    @router.post("/v1/videos/generations")
    @router.post("/api/video-tasks")
    def generate(body: dict = Body(...), authorization: str | None = Header(None), idempotency_key: str = Header("")):
        return runtime.submit(require_identity(authorization), body, idempotency_key)

    @router.get("/api/video-tasks")
    def videos(authorization: str | None = Header(None)):
        owner = require_identity(authorization)["id"]
        return {"items": [public(j) for j in get_store().list("video", owner)]}

    @router.get("/v1/videos/{video_id}")
    def video(video_id: str, authorization: str | None = Header(None)):
        return public(get_store().get("video", video_id, require_identity(authorization)["id"]))

    @router.post("/api/video-tasks/{video_id}/retry-download")
    def retry_download(video_id: str, authorization: str | None = Header(None)):
        return runtime.retry_download(require_identity(authorization)["id"], video_id)

    @router.post("/api/video-tasks/{video_id}/playback")
    def playback(video_id: str, authorization: str | None = Header(None)):
        owner = require_identity(authorization)["id"]
        job = get_store().get("video", video_id, owner)
        if job["status"] != "completed":
            raise HTTPException(409, "视频尚未完成")
        token = get_store().cipher().encrypt(json.dumps({"id": video_id, "owner": owner, "expires": time.time()+3600}).encode()).decode()
        return {"path": f"/v1/videos/{video_id}/content?ticket={quote(token)}"}

    @router.get("/v1/videos/{video_id}/content")
    @router.head("/v1/videos/{video_id}/content")
    def content(video_id: str, request: Request, ticket: str = "", authorization: str | None = Header(None)):
        if ticket:
            request.scope["query_string"] = b"ticket=REDACTED"
            try:
                value = json.loads(get_store().cipher().decrypt(ticket.encode(), ttl=3600))
                if value["id"] != video_id or value["expires"] < time.time():
                    raise ValueError()
                owner = value["owner"]
            except Exception as exc:
                raise HTTPException(403, "视频链接无效或已过期") from exc
        else:
            owner = require_identity(authorization)["id"]
        job = get_store().get("video", video_id, owner)
        if job["status"] != "completed" or not re.fullmatch(r"video_[0-9a-f]{32}", video_id):
            raise HTTPException(409, "视频尚未完成")
        path = get_store().directory / "videos" / (video_id + ".mp4")
        if not path.is_file():
            raise HTTPException(404, "视频文件已清理")
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})

    @router.get("/v1/responses/{response_id}")
    def response(response_id: str, authorization: str | None = Header(None)):
        value = get_store().get("response", response_id, require_identity(authorization)["id"])
        if value["expires_at"] < time.time():
            raise HTTPException(404, "响应已过期")
        return value["result"]

    @router.delete("/v1/responses/{response_id}")
    def delete_response(response_id: str, authorization: str | None = Header(None)):
        get_store().delete("response", response_id, require_identity(authorization)["id"])
        return {"id": response_id, "object": "response.deleted", "deleted": True}

    @router.get("/api/conversations")
    def histories(authorization: str | None = Header(None)):
        return {"items": get_store().list("conversation", require_identity(authorization)["id"])}

    @router.put("/api/conversations/{conversation_id}")
    def save_history(conversation_id: str, body: dict = Body(...), authorization: str | None = Header(None)):
        owner = require_identity(authorization)["id"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", conversation_id) or len(json.dumps(body)) > 4*1024*1024:
            raise HTTPException(400, "会话标识或内容无效")
        get_store().put("conversation", owner + ":" + conversation_id, owner, {**body, "id": conversation_id})
        return {"saved": True}

    @router.delete("/api/conversations/{conversation_id}")
    def delete_history(conversation_id: str, authorization: str | None = Header(None)):
        owner = require_identity(authorization)["id"]
        get_store().delete("conversation", owner + ":" + conversation_id, owner)
        return {"deleted": True}

    return router
