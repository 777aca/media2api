from __future__ import annotations

import os
from pathlib import Path
import threading
import time

from services.docker_update.engine import DockerEngine, replacement_config
from services.docker_update.protocol import (
    PROTOCOL, REPOSITORY, TERMINAL_STATES, UpdateError, read_json, validate_image, version_parts, write_json,
)


class UpdateWorker:
    def __init__(self, directory: Path, container: str, engine: DockerEngine, port: int = 80):
        self.directory, self.container, self.engine, self.port = directory, container, engine, port
        self.recovery_attempted = False

    def backup(self) -> dict:
        return read_json(self.directory / "backup.json")

    def status(self, job: dict, state: str, message: str) -> None:
        backup = self.backup()
        write_json(self.directory / "status.json", {
            "id": job["id"], "operation": job.get("operation"), "version": job.get("version"),
            "state": state, "message": message, "updated_at": time.time(),
            "rollback_version": backup.get("version"),
        })

    def heartbeat(self) -> None:
        try:
            self.engine.request("GET", "/_ping")
            ready = True
        except UpdateError:
            ready = False
        write_json(self.directory / "heartbeat.json", {"time": time.time(), "protocol": PROTOCOL, "ready": ready})

    def wait_healthy(self, container_id: str, version: str) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.engine.healthy(container_id, version, self.port):
                return
            time.sleep(2)
        raise UpdateError("新容器未在 120 秒内通过版本和启动检查")

    def original(self) -> tuple[dict, str]:
        info = self.engine.inspect(self.container)
        if not info or info.get("Config", {}).get("Labels", {}).get("io.media2api.updater.managed") != "true":
            raise UpdateError("目标容器未启用更新管理标签")
        if not info.get("State", {}).get("Running"):
            raise UpdateError("目标容器未运行，请先恢复服务")
        mounts = {mount.get("Destination") for mount in info.get("Mounts", [])}
        if not {"/app/data", "/app/config.json"} <= mounts:
            raise UpdateError("请先将 data 和 config.json 挂载到持久存储后再更新")
        version = info["Config"]["Labels"].get("org.opencontainers.image.version", "").removeprefix("v")
        version_parts(version)
        return info, version

    def restore(self, journal: dict) -> None:
        original = journal["original"]
        current = self.engine.inspect(self.container)
        if current:
            allowed = {original["Id"], journal.get("new_id")}
            expected_image = journal.get("image_id")
            if current.get("Id") not in allowed and current.get("Image") != expected_image:
                raise UpdateError("目标容器已被其他操作替换，已暂停自动恢复")
            if current.get("Id") == original["Id"]:
                if not current.get("State", {}).get("Running"):
                    self.engine.start(current["Id"])
                self.wait_healthy(current["Id"], journal["previous_version"])
                return
            self.engine.remove(current["Id"])
        restored_id = self.engine.create(self.container, replacement_config(original, original["Image"]))
        # Persist the ID so recovery can itself be recovered after another interruption.
        journal["new_id"] = restored_id
        write_json(self.directory / "journal.json", journal)
        self.engine.start(restored_id)
        self.wait_healthy(restored_id, journal["previous_version"])

    def execute(self, job: dict) -> None:
        journal = None
        try:
            if job.get("protocol") != PROTOCOL or job.get("operation") not in {"update", "rollback"}:
                raise UpdateError("不支持的更新任务")
            version_parts(job.get("version"))
            original, current_version = self.original()
            if job["operation"] == "update":
                if version_parts(job["version"]) <= version_parts(current_version):
                    raise UpdateError("目标版本必须高于当前容器版本")
                image = validate_image(job.get("image"))
                self.status(job, "pulling", "正在下载并校验新版本镜像，当前服务继续运行")
                self.engine.pull(image)
                image_info = self.engine.inspect_image(image)
                labels = image_info.get("Config", {}).get("Labels", {})
                if (labels.get("org.opencontainers.image.version", "").removeprefix("v") != job["version"]
                    or labels.get("org.opencontainers.image.source") != f"https://github.com/{REPOSITORY}"
                    or labels.get("io.media2api.update.protocol") != str(PROTOCOL)):
                    raise UpdateError("镜像来源、版本或更新协议校验失败")
                desired = original
            else:
                backup = self.backup()
                if backup.get("version") != job["version"] or not isinstance(backup.get("container"), dict):
                    raise UpdateError("没有匹配的上一版本备份")
                desired = backup["container"]
                image_info = self.engine.inspect_image(desired["Image"])
                image = image_info["Id"]
            payload = replacement_config(desired, image)
            # Use the new image's labels, preserving deployment-specific labels and Compose identity.
            if job["operation"] == "update":
                payload["Labels"] = {**(payload.get("Labels") or {}), **labels}
            journal = {"job": job, "original": original, "previous_version": current_version,
                       "image_id": image_info["Id"], "new_id": None}
            write_json(self.directory / "journal.json", journal)
            self.status(job, "restarting", "正在切换容器，服务将短暂中断")
            self.engine.stop(original["Id"])
            self.engine.remove(original["Id"])
            new_id = self.engine.create(self.container, payload)
            journal["new_id"] = new_id
            write_json(self.directory / "journal.json", journal)
            self.engine.start(new_id)
            self.status(job, "checking", "正在检查新容器的启动状态和版本")
            self.wait_healthy(new_id, job["version"])
            write_json(self.directory / "backup.json", {"version": current_version, "container": original})
            self.status(job, "succeeded", "更新完成，服务已恢复" if job["operation"] == "update" else "回滚完成，服务已恢复")
            (self.directory / "journal.json").unlink()
        except Exception as exc:
            message = str(exc) if isinstance(exc, UpdateError) else "更新过程异常，请检查更新服务和磁盘权限"
            if journal is not None:
                try:
                    self.status(job, "recovering", "操作未完成，正在恢复原版本")
                    self.restore(journal)
                    self.status(job, "rolled_back", f"{message}；已自动恢复原版本")
                    (self.directory / "journal.json").unlink(missing_ok=True)
                except Exception:
                    self.recovery_attempted = True
                    self.status(job, "failed", "自动恢复未完成，已保留恢复记录；请按更新文档在宿主机恢复")
            else:
                self.status(job, "failed", message)

    def run_once(self) -> None:
        journal = read_json(self.directory / "journal.json")
        if journal:
            if self.recovery_attempted:
                return
            self.recovery_attempted = True
            # After process/host interruption, recover first; never replay a destructive update.
            job = journal["job"]
            try:
                self.status(job, "recovering", "更新服务重新启动，正在恢复中断前的版本")
                self.restore(journal)
                self.status(job, "rolled_back", "更新曾被中断，已恢复原版本")
                (self.directory / "journal.json").unlink()
            except Exception:
                self.status(job, "failed", "自动恢复未完成，请按更新文档在宿主机恢复")
                return
        pending = self.directory / "pending"
        job = read_json(pending / "request.json")
        if not job:
            # A producer may have exited between reserving the directory and writing its request.
            if pending.is_dir() and time.time() - pending.stat().st_mtime > 60:
                try:
                    pending.rmdir()  # Only an empty, abandoned reservation can be removed.
                except OSError:
                    pass
            return
        status = read_json(self.directory / "status.json")
        if status.get("id") != job.get("id") or status.get("state") not in TERMINAL_STATES:
            self.execute(job)
        if not (self.directory / "journal.json").exists():
            (pending / "request.json").unlink(missing_ok=True)
            pending.rmdir()


def main() -> None:
    import fcntl  # The updater runs only in its Linux Docker container.

    directory = Path(os.getenv("MEDIA2API_UPDATE_DIR", "/state"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = UpdateWorker(directory, os.environ["MEDIA2API_UPDATE_CONTAINER"], DockerEngine(),
                              int(os.getenv("MEDIA2API_UPDATE_HEALTH_PORT", "80")))

        def heartbeat_loop():
            while True:
                try:
                    worker.heartbeat()
                except Exception:
                    print("[updater] 无法写入更新服务状态", flush=True)
                time.sleep(5)

        threading.Thread(target=heartbeat_loop, daemon=True).start()
        while True:
            try:
                worker.run_once()
            except Exception:
                print("[updater] 无法处理任务，请检查状态卷和 Docker 引擎", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
