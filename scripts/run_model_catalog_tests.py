"""在无真实配置、无真实账号、封锁出站网络的临时副本运行模型目录测试。

执行：.venv\\Scripts\\python.exe scripts\\run_model_catalog_tests.py
默认将结果保存到 logs/model-catalog-tests.log，可在命令后指定 unittest 名称。
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


DEFAULT_TESTS = [
    "test.test_model_catalog_service",
    "test.test_text_model_routing",
    "test.test_account_identity",
    "test.test_models_api",
    "test.test_model_catalog_protocol",
    "test.test_model_backend_parser",
    "test.test_codex_client",
    "test.test_codex_text_service",
    "test.test_codex_image_service",
    "test.test_codex_image_integration",
    "test.test_web_image_25",
    "test.test_codex_protocol_errors",
    "test.test_proxy_service",
    "test.test_account_export",
    "test.test_account_image_capabilities",
    "test.test_account_text_import_payload",
    "test.test_v1_models.ModelListTests.test_list_models_only_returns_image_models_backed_by_account_types",
    "test.test_v1_models.ModelListTests.test_list_models_does_not_return_codex_models_for_web_plus_accounts",
]


def isolated_child(test_names: list[str]) -> int:
    # 只有子进程到这里才导入项目及第三方库；其路径必须属于临时副本。
    from unittest import mock
    import socket

    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    # 模型目录与文本协议夹具使用确定的客户端版本，不触发 GitHub 版本发现。
    os.environ["MEDIA2API_CODEX_CLIENT_VERSION"] = "0.146.0"
    attempted_requests: list[str] = []
    attempts_lock = threading.Lock()
    local_control = threading.local()

    def blocked(*_args: object, **_kwargs: object) -> object:
        if getattr(local_control, "socket_pair", False):
            return None
        with attempts_lock:
            attempted_requests.append("blocked")
        raise AssertionError("offline test attempted an outbound network request")

    # Windows asyncio 的内部 socketpair 用回环连接实现，仅此控制通道允许。
    original_socketpair = socket.socketpair
    original_connect = socket.socket.connect

    def control_socketpair(*args: object, **kwargs: object) -> object:
        local_control.socket_pair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            local_control.socket_pair = False

    def connect_guard(sock: socket.socket, *args: object, **kwargs: object) -> object:
        if getattr(local_control, "socket_pair", False):
            return original_connect(sock, *args, **kwargs)
        return blocked(*args, **kwargs)

    with ExitStack() as patches:
        import requests
        import httpx
        from curl_cffi import requests as curl_requests

        for owner, method in (
            (requests.Session, "request"),
            (curl_requests.Session, "request"),
            (curl_requests.AsyncSession, "request"),
            (httpx.HTTPTransport, "handle_request"),
            (httpx.AsyncHTTPTransport, "handle_async_request"),
        ):
            # 显式 object patch 保留 TestClient 的内存 transport。
            patches.enter_context(mock.patch.object(owner, method, side_effect=blocked))
        patches.enter_context(mock.patch.object(socket.socket, "connect", connect_guard))
        patches.enter_context(mock.patch.object(socket.socket, "connect_ex", side_effect=blocked))
        patches.enter_context(mock.patch.object(socket, "create_connection", side_effect=blocked))
        patches.enter_context(mock.patch.object(socket, "socketpair", control_socketpair))
        suite = unittest.defaultTestLoader.loadTestsFromNames(test_names)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        print(f"\nOutbound network attempts: {len(attempted_requests)}")
        return 0 if result.wasSuccessful() and not attempted_requests else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*")
    parser.add_argument("--isolated-child", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    test_names = arguments.tests or DEFAULT_TESTS
    if arguments.isolated_child:
        return isolated_child(test_names)

    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="media2api-model-catalog-isolated-") as temporary_directory:
        isolated = Path(temporary_directory)
        for directory in ("api", "services", "utils", "test"):
            shutil.copytree(project / directory, isolated / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (isolated / "scripts").mkdir()
        shutil.copy2(Path(__file__), isolated / "scripts" / Path(__file__).name)
        shutil.copy2(project / "VERSION", isolated / "VERSION")
        settings = json.loads((project / "config.example.json").read_text(encoding="utf-8-sig"))
        settings["auth-key"] = "account-import-test-auth"
        (isolated / "config.json").write_text(json.dumps(settings), encoding="utf-8")
        environment = dict(os.environ)
        environment.update({"MEDIA2API_AUTH_KEY": "account-import-test-auth", "STORAGE_BACKEND": "json",
                            "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(isolated)})
        execution = subprocess.run(
            [sys.executable, str(isolated / "scripts" / Path(__file__).name), "--isolated-child", *test_names],
            cwd=isolated, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        output = execution.stdout + execution.stderr
        log_directory = project / "logs"
        log_directory.mkdir(exist_ok=True)
        (log_directory / "model-catalog-tests.log").write_text(output, encoding="utf-8")
        print(output, end="")
        return execution.returncode


if __name__ == "__main__":
    raise SystemExit(main())
