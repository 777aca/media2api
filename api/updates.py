from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from api.support import require_admin
from services.docker_update.protocol import UpdateError
from services.update_service import UpdateService


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str


def create_router(current_version: str) -> APIRouter:
    router = APIRouter(prefix="/api/system/update")
    service = UpdateService(current_version)

    @router.get("")
    async def get_update(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(service.check)

    @router.post("/check")
    async def check_update(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(service.check, True)

    @router.get("/status")
    async def get_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return service.status()

    async def submit(operation: str, body: UpdateRequest, authorization: str | None):
        require_admin(authorization)
        try:
            return await run_in_threadpool(service.submit, operation, body.version)
        except UpdateError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc

    @router.post("/apply", status_code=202)
    async def apply_update(body: UpdateRequest, authorization: str | None = Header(default=None)):
        return await submit("update", body, authorization)

    @router.post("/rollback", status_code=202)
    async def rollback(body: UpdateRequest, authorization: str | None = Header(default=None)):
        return await submit("rollback", body, authorization)

    return router
