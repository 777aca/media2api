from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services.docker_update.protocol import (
    PROTOCOL, REPOSITORY, UpdateError, enqueue, public_job, read_json,
    validate_image, version_parts,
)

RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPOSITORY}/releases"


def fetch_json(url: str) -> object:
    request = Request(url, headers={"User-Agent": "media2api-update-check", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read(512 * 1024 + 1)
        if len(payload) > 512 * 1024:
            raise UpdateError("版本信息超过大小限制")
        return json.loads(payload)
    except HTTPError as exc:
        if exc.code == 404 and url == RELEASE_URL:
            return None
        if exc.code in {403, 429}:
            raise UpdateError("GitHub 暂时限制了版本查询，请稍后重试") from exc
        raise UpdateError("无法获取 GitHub 发布信息") from exc
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        raise UpdateError("无法连接 GitHub 或发布信息格式无效") from exc


def fetch_latest_release() -> dict | None:
    release = fetch_json(RELEASE_URL)
    if release is None:
        return None
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise UpdateError("GitHub 未返回有效的正式版本")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("发布标签必须使用 v 开头的正式版本号")
    version = tag[1:]
    version_parts(version)
    assets = release.get("assets")
    asset_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/media2api-release.json"
    if not isinstance(assets, list) or not any(
        isinstance(asset, dict) and asset.get("name") == "media2api-release.json"
        and asset.get("browser_download_url") == asset_url for asset in assets
    ):
        raise UpdateError("最新发布缺少可更新镜像清单，请等待镜像发布完成")
    manifest = fetch_json(asset_url)
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
        or manifest.get("schema") != PROTOCOL or manifest.get("version") != version):
        raise UpdateError("发布清单与版本不一致或暂不支持")
    image = validate_image(manifest.get("image"))
    return {"version": version, "image": image, "url": f"{RELEASE_PAGE}/tag/{tag}",
            "body": str(release.get("body") or "")[:30000],
            "published_at": str(release.get("published_at") or "")}


class UpdateService:
    def __init__(self, current_version: str, directory: Path | None = None):
        self.current_version = current_version
        configured = os.getenv("MEDIA2API_UPDATE_DIR", "").strip()
        self.directory = directory if directory is not None else (Path(configured) if configured else None)
        self._lock = Lock()
        self._latest: dict | None = None
        self._checked_at: float | None = None
        self._attempted_at = 0.0
        self._error: str | None = None

    def check(self, force: bool = False) -> dict:
        # A single check is shared by simultaneous browser requests, including force checks.
        requested_at = time.time()
        with self._lock:
            if self._attempted_at < requested_at and (force or requested_at - self._attempted_at >= 300):
                try:
                    self._latest = fetch_latest_release()
                    self._checked_at = time.time()
                    self._error = None
                except UpdateError as exc:
                    self._error = str(exc)
                self._attempted_at = time.time()
            return self.status()

    def status(self) -> dict:
        supported = False
        reason = "当前为源码运行；一键更新需要启用 Docker 更新服务"
        job = None
        rollback_version = None
        if self.directory is not None:
            try:
                heartbeat = read_json(self.directory / "heartbeat.json")
                stamp = heartbeat.get("time")
                supported = (heartbeat.get("protocol") == PROTOCOL and isinstance(stamp, (int, float))
                             and 0 <= time.time() - stamp < 30 and heartbeat.get("ready") is True)
                reason = "" if supported else "Docker 更新服务未就绪，请检查 updater 容器"
                status = read_json(self.directory / "status.json")
                job = public_job(status)
                rollback_version = status.get("rollback_version")
                if rollback_version is not None:
                    version_parts(rollback_version)
                    if rollback_version == self.current_version:
                        rollback_version = None
                pending = read_json(self.directory / "pending" / "request.json")
                if pending and pending.get("id") != status.get("id"):
                    job = public_job({**pending, "state": "queued", "message": "等待更新服务接收任务",
                                      "updated_at": pending.get("created_at")})
            except UpdateError as exc:
                supported, reason = False, str(exc)
        latest = self._latest
        return {"current_version": self.current_version, "latest": latest,
                "has_update": bool(latest and version_parts(latest["version"]) > version_parts(self.current_version)),
                "checked_at": self._checked_at, "error": self._error, "supported": supported,
                "reason": reason, "deployment": "docker" if self.directory else "source",
                "job": job, "rollback_version": rollback_version, "releases_url": RELEASE_PAGE}

    def submit(self, operation: str, version: str) -> dict:
        version_parts(version)
        with self._lock:
            state = self.status()
            if not state["supported"] or self.directory is None:
                raise UpdateError(state["reason"])
            image = ""
            if operation == "update":
                # Revalidate the selected release before accepting a write operation.
                latest = fetch_latest_release()
                if not latest or latest["version"] != version:
                    raise UpdateError("发布版本已变化，请重新检查更新")
                if version_parts(version) <= version_parts(self.current_version):
                    raise UpdateError("所选版本不高于当前版本")
                image = latest["image"]
            elif operation != "rollback" or version != state["rollback_version"]:
                raise UpdateError("没有可回滚的上一版本")
            try:
                enqueue(self.directory, operation, version, image)
            except OSError as exc:
                raise UpdateError("无法写入更新任务，请检查共享卷权限") from exc
            return self.status()
