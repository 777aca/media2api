from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Event

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api import accounts, ai, image_tasks, system, updates, generation_runtime, grok_native
from api.errors import install_exception_handlers
from api.support import resolve_web_asset, start_limited_account_watcher
from services.backup_service import backup_service
from services.config import config
from services.image_service import start_image_cleanup_scheduler
from services.quota_alerts import get_quota_alert_service


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        from services.generation_runtime import get_generation_runtime
        runtime = get_generation_runtime()
        runtime.start()
        from services.providers.grok_video import runtime as video_runtime
        video_runtime.start()
        stop_event = Event()
        thread = start_limited_account_watcher(stop_event)
        cleanup_thread = start_image_cleanup_scheduler(stop_event)
        backup_service.start()
        get_quota_alert_service().start()
        config.cleanup_old_images()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=1)
            cleanup_thread.join(timeout=1)
            backup_service.stop()
            from fastapi.concurrency import run_in_threadpool
            await run_in_threadpool(get_quota_alert_service().stop)
            await run_in_threadpool(runtime.stop)
            await run_in_threadpool(video_runtime.stop)

    app = FastAPI(title="media2api", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Image-Task-Id"],
    )
    app.include_router(generation_runtime.create_router())
    app.include_router(ai.create_router())
    app.include_router(grok_native.create_router())
    # Compatibility aliases run the same native handlers and authentication.
    from fastapi.routing import APIRoute
    for route in list(app.routes):
        if isinstance(route, APIRoute) and route.path.startswith("/v1/"):
            app.add_api_route("/grok" + route.path, route.endpoint, methods=list(route.methods), include_in_schema=False)
    app.include_router(accounts.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(system.create_router(app_version))
    app.include_router(updates.create_router(app_version))

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        clean_path = full_path.strip("/")
        if clean_path.split("/", 1)[0] in {"api", "grok", "v1", "debug"}:
            raise HTTPException(status_code=404, detail="Not Found")
        asset = resolve_web_asset(full_path)
        if asset is not None:
            return FileResponse(asset)
        if clean_path.startswith("_next/"):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(fallback)

    return app
