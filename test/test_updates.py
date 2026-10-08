from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.updates as api_module
from services.docker_update.engine import replacement_config
import services.docker_update.engine as engine_module
from services.docker_update.protocol import IMAGE, UpdateError, enqueue, read_json, write_json
from services.docker_update.worker import UpdateWorker
import services.update_service as service_module

IMAGE_REF = IMAGE + "@sha256:" + "a" * 64
LATEST = {"version": "0.2.0", "image": IMAGE_REF, "url": "https://github.com/777aca/media2api/releases/tag/v0.2.0", "body": "更新", "published_at": "2026-10-08"}


class UpdateServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        write_json(self.root / "heartbeat.json", {"time": time.time(), "protocol": 1, "ready": True})
        self.service = service_module.UpdateService("0.1.0", self.root)

    def test_cache_force_expiry_and_failure_keep_last_success(self):
        with mock.patch.object(service_module, "fetch_latest_release", return_value=LATEST) as fetch:
            self.assertTrue(self.service.check()["has_update"])
            self.service.check()
            self.assertEqual(fetch.call_count, 1)
            self.service.check(True)
            self.assertEqual(fetch.call_count, 2)
            self.service._attempted_at -= 301
            self.service.check()
            self.assertEqual(fetch.call_count, 3)
        previous = self.service.status()["checked_at"]
        with mock.patch.object(service_module, "fetch_latest_release", side_effect=UpdateError("网络错误")):
            state = self.service.check(True)
        self.assertEqual(state["latest"], LATEST)
        self.assertEqual(state["checked_at"], previous)
        self.assertEqual(state["error"], "网络错误")

    def test_first_failure_and_no_releases(self):
        with mock.patch.object(service_module, "fetch_latest_release", side_effect=UpdateError("格式错误")):
            state = self.service.check(True)
        self.assertIsNone(state["latest"])
        self.assertIsNone(state["checked_at"])
        self.assertFalse(state["has_update"])
        with mock.patch.object(service_module, "fetch_latest_release", return_value=None):
            self.assertIsNone(self.service.check(True)["error"])

    def test_concurrent_force_checks_share_network_work(self):
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            release.wait(2)
            return LATEST

        with mock.patch.object(service_module, "fetch_latest_release", side_effect=fetch) as call:
            first = threading.Thread(target=self.service.check, args=(True,))
            second = threading.Thread(target=self.service.check, args=(True,))
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            time.sleep(0.03)
            release.set()
            first.join()
            second.join()
            self.assertEqual(call.call_count, 1)

    def test_stale_worker_and_source_do_not_accept_updates(self):
        write_json(self.root / "heartbeat.json", {"time": time.time() - 31, "protocol": 1, "ready": True})
        with self.assertRaisesRegex(UpdateError, "未就绪"):
            self.service.submit("update", "0.2.0")
        with mock.patch.dict("os.environ", {"MEDIA2API_UPDATE_DIR": ""}):
            source = service_module.UpdateService("0.1.0")
        self.assertFalse(source.status()["supported"])
        with self.assertRaisesRegex(UpdateError, "源码"):
            source.submit("update", "0.2.0")

    def test_enqueue_deduplicates_and_never_exposes_inspection(self):
        write_json(self.root / "backup.json", {"secret": "synthetic-only"})
        with mock.patch.object(service_module, "fetch_latest_release", return_value=LATEST):
            state = self.service.submit("update", "0.2.0")
            self.assertEqual(state["job"]["state"], "queued")
            with self.assertRaisesRegex(UpdateError, "已有"):
                self.service.submit("update", "0.2.0")
        self.assertNotIn("synthetic-only", json.dumps(state))
        self.assertNotIn("image", state["job"])

    def test_reject_stale_release_or_unavailable_rollback(self):
        with mock.patch.object(service_module, "fetch_latest_release", return_value=LATEST):
            with self.assertRaisesRegex(UpdateError, "已变化"):
                self.service.submit("update", "0.3.0")
        with self.assertRaisesRegex(UpdateError, "回滚"):
            self.service.submit("rollback", "0.0.1")

    def test_manifest_validation_and_repository_restriction(self):
        release = {"tag_name": "v0.2.0", "draft": False, "prerelease": False, "assets": [{
            "name": "media2api-release.json", "browser_download_url": "https://github.com/777aca/media2api/releases/download/v0.2.0/media2api-release.json"}]}
        manifest = {"schema": 1, "version": "0.2.0", "image": IMAGE_REF}
        with mock.patch.object(service_module, "fetch_json", side_effect=[release, manifest]):
            self.assertEqual(service_module.fetch_latest_release()["image"], IMAGE_REF)
        for invalid in ({**manifest, "image": "ghcr.io/other/image:latest"}, {**manifest, "version": "0.3.0"}, {**manifest, "schema": 2}):
            with mock.patch.object(service_module, "fetch_json", side_effect=[release, invalid]):
                with self.assertRaises(UpdateError):
                    service_module.fetch_latest_release()
        with mock.patch.object(service_module, "fetch_json", return_value={**release, "assets": []}):
            with self.assertRaisesRegex(UpdateError, "缺少"):
                service_module.fetch_latest_release()

    def test_admin_auth_and_payload(self):
        app = FastAPI()
        with mock.patch.object(api_module, "UpdateService", return_value=self.service):
            app.include_router(api_module.create_router("0.1.0"))
        client = TestClient(app)
        paths = [("GET", ""), ("GET", "/status"), ("POST", "/check"), ("POST", "/apply"), ("POST", "/rollback")]
        for method, path in paths:
            kwargs = {"json": {"version": "0.2.0"}} if path in {"/apply", "/rollback"} else {}
            self.assertEqual(client.request(method, f"/api/system/update{path}", **kwargs).status_code, 401)
        with mock.patch("api.support.require_identity", return_value={"role": "user"}):
            self.assertEqual(client.get("/api/system/update/status").status_code, 403)
        headers = {"Authorization": "Bearer account-import-test-auth"}
        with mock.patch.object(service_module, "fetch_latest_release", return_value=LATEST):
            self.assertEqual(client.post("/api/system/update/apply", headers=headers, json={"version": "0.2.0"}).status_code, 202)
            self.assertEqual(client.post("/api/system/update/apply", headers=headers, json={"version": "0.2.0"}).status_code, 409)
            self.assertEqual(client.post("/api/system/update/apply", headers=headers, json={"version": "0.2.0", "image": "evil"}).status_code, 422)


def container_info(version="0.1.0"):
    return {
        "Id": "original-id", "Image": "sha256:old", "State": {"Running": True},
        "Config": {"Image": IMAGE + ":latest", "Env": ["SYNTHETIC_SECRET=keep"], "Labels": {
            "io.media2api.updater.managed": "true", "org.opencontainers.image.version": version,
            "com.docker.compose.service": "app"}, "Cmd": ["uv", "run", "uvicorn"], "ExposedPorts": {"80/tcp": {}}},
        "HostConfig": {"Binds": ["/host/data:/app/data", "/host/config.json:/app/config.json", "state:/run/media2api-update"], "RestartPolicy": {"Name": "unless-stopped"}, "PortBindings": {"80/tcp": [{"HostPort": "3000"}]}, "NetworkMode": "media_default"},
        "Mounts": [{"Destination": "/app/data", "Type": "bind"}, {"Destination": "/app/config.json", "Type": "bind"}, {"Destination": "/cache", "Type": "volume", "Name": "anonymous-id", "RW": True}],
        "NetworkSettings": {"Networks": {"media_default": {"Aliases": ["app", "media2api"], "IPAddress": "172.18.0.2"}}},
    }


class FakeEngine:
    def __init__(self):
        self.current = container_info()
        self.calls = []
        self.fail_pull = False
        self.fail_health = False

    def inspect(self, _name):
        return copy.deepcopy(self.current)

    def inspect_image(self, image):
        return {"Id": "sha256:old" if image == "sha256:old" else "sha256:new", "Config": {"Labels": {
            "org.opencontainers.image.version": "0.2.0", "org.opencontainers.image.source": "https://github.com/777aca/media2api", "io.media2api.update.protocol": "1"}}}

    def pull(self, _image):
        self.calls.append("pull")
        if self.fail_pull:
            raise UpdateError("下载失败")

    def stop(self, _id):
        self.calls.append("stop")
        self.current["State"]["Running"] = False

    def remove(self, _id):
        self.calls.append("remove")
        self.current = None

    def create(self, _name, payload):
        self.calls.append("create")
        self.current = container_info(payload["Labels"]["org.opencontainers.image.version"])
        self.current.update({"Id": f"created-{len(self.calls)}", "Config": payload, "Image": "sha256:old" if payload["Image"] == "sha256:old" else "sha256:new"})
        return self.current["Id"]

    def start(self, _id):
        self.calls.append("start")
        self.current["State"]["Running"] = True


class UpdateWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = FakeEngine()
        self.worker = UpdateWorker(self.root, "media2api", self.engine)
        self.worker.wait_healthy = mock.Mock()
        self.job = enqueue(self.root, "update", "0.2.0", IMAGE_REF)

    def test_update_preserves_data_environment_ports_and_creates_rollback_point(self):
        self.worker.run_once()
        self.assertEqual(read_json(self.root / "status.json")["state"], "succeeded")
        self.assertEqual(self.engine.calls, ["pull", "stop", "remove", "create", "start"])
        config = self.engine.current["Config"]
        self.assertEqual(config["Env"], ["SYNTHETIC_SECRET=keep"])
        self.assertEqual(config["HostConfig"]["PortBindings"]["80/tcp"][0]["HostPort"], "3000")
        self.assertIn("anonymous-id:/cache:rw", config["HostConfig"]["Binds"])
        self.assertEqual(config["NetworkingConfig"]["EndpointsConfig"]["media_default"]["Aliases"], ["app", "media2api"])
        self.assertEqual(read_json(self.root / "backup.json")["version"], "0.1.0")
        self.assertNotIn("SYNTHETIC_SECRET", (self.root / "status.json").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "pending").exists())
        self.assertFalse((self.root / "journal.json").exists())

    def test_pull_failure_does_not_stop_current_service(self):
        self.engine.fail_pull = True
        self.worker.run_once()
        self.assertEqual(self.engine.calls, ["pull"])
        self.assertEqual(read_json(self.root / "status.json")["state"], "failed")

    def test_wrong_image_metadata_does_not_replace_service(self):
        with mock.patch.object(self.engine, "inspect_image", return_value={"Id": "unexpected", "Config": {"Labels": {}}}):
            self.worker.run_once()
        self.assertEqual(self.engine.calls, ["pull"])
        self.assertIn("校验失败", read_json(self.root / "status.json")["message"])

    def test_create_failure_recovers_removed_container(self):
        create = self.engine.create
        attempts = 0

        def fail_once(name, payload):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise UpdateError("创建失败")
            return create(name, payload)

        with mock.patch.object(self.engine, "create", side_effect=fail_once):
            self.worker.run_once()
        self.assertEqual(read_json(self.root / "status.json")["state"], "rolled_back")
        self.assertEqual(self.engine.current["Image"], "sha256:old")

    def test_health_failure_restores_previous_version(self):
        self.worker.wait_healthy.side_effect = [UpdateError("启动失败"), None]
        self.worker.run_once()
        self.assertEqual(self.engine.current["Config"]["Labels"]["org.opencontainers.image.version"], "0.1.0")
        self.assertEqual(read_json(self.root / "status.json")["state"], "rolled_back")

    def test_manual_rollback_and_repeated_poll_are_idempotent(self):
        self.worker.run_once()
        enqueue(self.root, "rollback", "0.1.0")
        self.worker.run_once()
        self.assertEqual(self.engine.current["Config"]["Labels"]["org.opencontainers.image.version"], "0.1.0")
        calls = list(self.engine.calls)
        self.worker.run_once()
        self.assertEqual(self.engine.calls, calls)

    def test_worker_restart_recovers_journal_without_replaying_update(self):
        original = self.engine.inspect("media2api")
        write_json(self.root / "journal.json", {"job": self.job, "original": original, "previous_version": "0.1.0", "image_id": "sha256:new", "new_id": None})
        self.engine.current = None
        self.worker.run_once()
        self.assertNotIn("pull", self.engine.calls)
        self.assertEqual(read_json(self.root / "status.json")["state"], "rolled_back")
        self.assertFalse((self.root / "pending").exists())

    def test_failed_recovery_is_not_retried_in_a_tight_loop(self):
        self.worker.wait_healthy.side_effect = UpdateError("启动失败")
        self.worker.run_once()
        count = len(self.engine.calls)
        self.worker.run_once()
        self.assertEqual(len(self.engine.calls), count)
        self.assertTrue((self.root / "journal.json").exists())

    def test_reject_non_persistent_or_unmanaged_container(self):
        self.engine.current["Mounts"] = []
        self.worker.run_once()
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(read_json(self.root / "status.json")["state"], "failed")

    def test_replacement_rejects_unsupported_network_and_auto_remove(self):
        for host in ({"AutoRemove": True}, {"NetworkMode": "container:other"}):
            original = container_info()
            original["HostConfig"].update(host)
            with self.assertRaises(UpdateError):
                replacement_config(original, IMAGE_REF)


class DockerTransportTests(unittest.TestCase):
    def test_ping_plaintext_and_exec_stream_do_not_require_json(self):
        for content_type, payload in (("text/plain", b"OK"), ("application/vnd.docker.raw-stream", b"\x01\x00\x00\x00stdout")):
            connection = mock.Mock()
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = payload
            response.getheader.return_value = content_type
            with mock.patch.object(engine_module, "UnixConnection", return_value=connection):
                self.assertEqual(engine_module.DockerEngine().request("GET", "/_ping"), {})
            connection.close.assert_called_once()

    def test_docker_error_does_not_return_sensitive_raw_response(self):
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status = 500
        response.read.return_value = b'{"message":"synthetic-secret"}'
        with mock.patch.object(engine_module, "UnixConnection", return_value=connection):
            with self.assertRaises(UpdateError) as error:
                engine_module.DockerEngine().request("POST", "/containers/create", {})
        self.assertNotIn("synthetic-secret", str(error.exception))

    def test_pull_stream_errors_are_not_treated_as_success(self):
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.readline.side_effect = [b'{"status":"Downloading"}\n', b'{"errorDetail":{"message":"denied"}}\n', b'']
        with mock.patch.object(engine_module, "UnixConnection", return_value=connection):
            with self.assertRaisesRegex(UpdateError, "拉取镜像失败"):
                engine_module.DockerEngine().pull(IMAGE_REF)

    def test_health_requires_completed_exec_with_zero_exit_code(self):
        engine = engine_module.DockerEngine()
        for result, expected in (({"Running": False, "ExitCode": 0}, True), ({"Running": False, "ExitCode": 1}, False), ({"Running": True, "ExitCode": 0}, False)):
            with mock.patch.object(engine, "request", side_effect=[{"Id": "exec-id"}, {}, result]):
                self.assertEqual(engine.healthy("container-id", "0.2.0", 80), expected)
