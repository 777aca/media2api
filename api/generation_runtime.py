from __future__ import annotations

from fastapi import APIRouter, Header, Query
from fastapi.concurrency import run_in_threadpool

from api.support import require_identity, require_admin
from services.generation_runtime import get_generation_runtime
from services.generation_statistics import runtime_statistics


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/image-quota")
    async def quota(authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        result = get_generation_runtime().store.quota(str(identity["id"]))
        if identity.get("role") == "admin":
            result.update(image_quota_limit=None, image_quota_remaining=None)
        return result

    @router.post("/api/image-tasks/{task_id}/cancel")
    async def cancel(task_id: str, authorization: str | None = Header(default=None)):
        return await run_in_threadpool(get_generation_runtime().change_task, require_identity(authorization), task_id, "cancel")

    @router.post("/api/runtime/tasks/{task_id}/end")
    async def end(task_id: str, authorization: str | None = Header(default=None)):
        identity = require_admin(authorization)
        return await run_in_threadpool(get_generation_runtime().change_task, identity, task_id, "end")

    @router.get("/api/runtime/tasks")
    async def tasks(authorization: str | None = Header(default=None)):
        identity = require_admin(authorization)
        return {"items": get_generation_runtime().jobs(identity, all_users=True)}

    @router.get("/api/runtime/statistics")
    async def statistics(days: int = Query(default=1, ge=1, le=30), model: str = "", channel: str = "", key_id: str = "", authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from services.account_service import account_service
        return await run_in_threadpool(runtime_statistics, get_generation_runtime(), days=days, model=model, channel=channel, key_id=key_id, accounts=account_service.list_accounts())

    @router.post("/api/accounts/{account_id}/clear-image-cooldown")
    async def clear(account_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from services.account_service import account_service
        from services.generation_errors import GenerationRuntimeError
        account = next((item for item in account_service.list_accounts() if item.get("pool_account_id") == account_id), None)
        if not account:
            raise GenerationRuntimeError("账号不存在", "account_not_found", 404)
        account_service.clear_image_blocks(account["access_token"])
        return {"ok": True}

    return router
