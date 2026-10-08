from __future__ import annotations

import copy
import http.client
import json
import socket
import time
from urllib.parse import quote, urlencode

from services.docker_update.protocol import UpdateError


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, timeout: int = 30):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


class DockerEngine:
    def request(self, method: str, path: str, payload: dict | None = None, timeout: int = 30,
                missing_ok: bool = False) -> object:
        connection = UnixConnection(timeout)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024)
            if missing_ok and response.status == 404:
                return None
            if not 200 <= response.status < 300:
                raise UpdateError(f"Docker 操作失败（HTTP {response.status}）")
            if not raw or "application/json" not in response.getheader("Content-Type", ""):
                return {}
            return json.loads(raw)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise UpdateError("无法连接 Docker 引擎或响应格式无效") from exc
        finally:
            connection.close()

    def inspect(self, name: str) -> dict | None:
        value = self.request("GET", f"/containers/{quote(name, safe='')}/json", missing_ok=True)
        if value is not None and not isinstance(value, dict):
            raise UpdateError("Docker 容器信息格式无效")
        return value

    def inspect_image(self, image: str) -> dict:
        value = self.request("GET", f"/images/{quote(image, safe='')}/json")
        if not isinstance(value, dict) or not isinstance(value.get("Id"), str):
            raise UpdateError("Docker 镜像信息格式无效")
        return value

    def pull(self, image: str) -> None:
        connection = UnixConnection(900)
        try:
            connection.request("POST", "/images/create?" + urlencode({"fromImage": image}))
            response = connection.getresponse()
            if response.status != 200:
                raise UpdateError(f"拉取镜像失败（HTTP {response.status}）；请检查 GHCR 镜像访问权限")
            total = 0
            deadline = time.monotonic() + 900
            while line := response.readline(1024 * 1024):
                total += len(line)
                if total > 32 * 1024 * 1024 or time.monotonic() > deadline:
                    raise UpdateError("拉取镜像超时或响应过大")
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("error") or value.get("errorDetail"):
                    raise UpdateError("拉取镜像失败，请检查网络、磁盘空间及镜像访问权限")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise UpdateError("拉取镜像时连接中断") from exc
        finally:
            connection.close()

    def remove(self, container_id: str) -> None:
        # v=false preserves both named and anonymous volumes.
        self.request("DELETE", f"/containers/{quote(container_id, safe='')}?force=true&v=false")

    def stop(self, container_id: str) -> None:
        self.request("POST", f"/containers/{quote(container_id, safe='')}/stop?t=30", timeout=40)

    def create(self, name: str, payload: dict) -> str:
        value = self.request("POST", "/containers/create?" + urlencode({"name": name}), payload)
        if not isinstance(value, dict) or not isinstance(value.get("Id"), str):
            raise UpdateError("Docker 未返回新容器标识")
        return value["Id"]

    def start(self, container_id: str) -> None:
        self.request("POST", f"/containers/{quote(container_id, safe='')}/start")

    def healthy(self, container_id: str, version: str, port: int) -> bool:
        # Probe inside the container: no dependency on DNS, mapped host ports or extra curl binary.
        code = ("import json,sys,urllib.request; "
                "r=urllib.request.urlopen('http://127.0.0.1:'+sys.argv[1]+'/version',timeout=4); "
                "sys.exit(0 if json.load(r).get('version')==sys.argv[2] else 1)")
        try:
            value = self.request("POST", f"/containers/{container_id}/exec", {
                "Cmd": ["python", "-c", code, str(port), version], "AttachStdout": True, "AttachStderr": True,
            })
            if not isinstance(value, dict) or not isinstance(value.get("Id"), str):
                return False
            exec_id = quote(value["Id"], safe="")
            self.request("POST", f"/exec/{exec_id}/start", {"Detach": False, "Tty": False}, timeout=10)
            result = self.request("GET", f"/exec/{exec_id}/json")
            return isinstance(result, dict) and result.get("Running") is False and result.get("ExitCode") == 0
        except UpdateError:
            return False


def replacement_config(original: dict, image: str) -> dict:
    config = copy.deepcopy(original["Config"])
    host = copy.deepcopy(original["HostConfig"])
    if str(host.get("NetworkMode", "")).startswith("container:") or host.get("AutoRemove"):
        raise UpdateError("暂不支持共享其他容器网络或自动删除模式")
    config["Image"] = image
    # Docker-generated hostname must not be carried to a different container.
    if config.get("Hostname") == original.get("Id", "")[:12]:
        config.pop("Hostname", None)
    config.pop("MacAddress", None)
    # Pin anonymous volumes too; a recreate must never replace persisted data with empty volumes.
    binds = host.get("Binds") or []
    destinations = {str(bind).split(":")[1] for bind in binds if ":" in str(bind)}
    destinations.update(mount.get("Target") for mount in host.get("Mounts") or [])
    for mount in original.get("Mounts", []):
        if mount.get("Type") == "volume" and mount.get("Destination") not in destinations:
            binds.append(f"{mount['Name']}:{mount['Destination']}:{'rw' if mount.get('RW') else 'ro'}")
    host["Binds"] = binds
    config["HostConfig"] = host
    if host.get("NetworkMode") not in {"host", "none"}:
        endpoints = {}
        for name, network in original.get("NetworkSettings", {}).get("Networks", {}).items():
            aliases = [alias for alias in network.get("Aliases") or []
                       if alias not in {original.get("Id"), original.get("Id", "")[:12]}]
            endpoints[name] = {"Aliases": aliases}
            for field in ("IPAMConfig", "DriverOpts"):
                if network.get(field):
                    endpoints[name][field] = network[field]
        config["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return config
