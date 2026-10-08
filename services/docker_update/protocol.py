from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import uuid

REPOSITORY = "777aca/media2api"
IMAGE = f"ghcr.io/{REPOSITORY}"
PROTOCOL = 1
TERMINAL_STATES = {"succeeded", "rolled_back", "failed"}


class UpdateError(Exception):
    pass


def version_parts(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise UpdateError("版本号格式无效")
    return tuple(int(part) for part in value.split("."))


def validate_image(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(re.escape(IMAGE) + r"@sha256:[a-f0-9]{64}", value):
        raise UpdateError("更新镜像必须来自本项目且包含固定摘要")
    return value


def read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise UpdateError("更新状态文件过大")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        raise UpdateError("无法读取更新状态") from exc
    if not isinstance(value, dict):
        raise UpdateError("更新状态格式无效")
    return value


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def enqueue(directory: Path, operation: str, version: str, image: str = "") -> dict:
    version_parts(version)
    if operation not in {"update", "rollback"}:
        raise UpdateError("更新操作无效")
    if operation == "update":
        validate_image(image)
    pending = directory / "pending"
    try:
        pending.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise UpdateError("已有更新或回滚任务正在执行，请等待完成") from exc
    job = {"id": uuid.uuid4().hex, "operation": operation, "version": version,
           "image": image, "protocol": PROTOCOL, "created_at": time.time()}
    try:
        write_json(pending / "request.json", job)
    except Exception:
        pending.rmdir()
        raise
    return job


def public_job(value: dict) -> dict | None:
    if not value or not isinstance(value.get("id"), str):
        return None
    # Never return the journal, container inspection or environment to the web API.
    return {key: value.get(key) for key in (
        "id", "operation", "version", "state", "message", "updated_at",
    )}
