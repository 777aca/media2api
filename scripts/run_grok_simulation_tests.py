"""Exercise Grok forwarding against a local Postman simulation and a WebSocket echo server.

The child runs from a temporary source copy with example configuration and no real accounts.
Start the simulation first; pass its grok route URL as the sole argument.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time


def child(base_url: str) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import httpx
    import uvicorn
    from websockets.asyncio.server import serve
    from api import grok
    from api.errors import install_exception_handlers

    os.environ["MEDIA2API_GROK_GATEWAY_URL"] = base_url
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(grok.create_router())
    headers = {"Authorization": "Bearer synthetic-grok-key"}
    with TestClient(app) as client:
        assert client.get("/grok/v1/models").status_code == 401
        models = client.get("/grok/v1/models", headers=headers)
        assert models.status_code == 200 and models.json()["data"][0]["id"] == "grok-chat-fast"
        chat = client.post("/grok/v1/chat/completions", headers=headers,
                           json={"model": "grok-chat-fast", "messages": [], "stream": True})
        assert chat.status_code == 200 and "data: [DONE]" in chat.text
        response = client.post("/grok/v1/responses", headers=headers, json={"model": "grok-chat-fast", "input": "test"})
        assert response.status_code == 200 and response.json()["id"] == "resp_mock"
        assert client.get("/grok/v1/responses/resp_mock", headers=headers).json()["id"] == "resp_mock"
        assert client.delete("/grok/v1/responses/resp_mock", headers=headers).json()["deleted"]
        message = client.post("/grok/v1/messages", headers={"x-api-key": "synthetic-grok-key", "anthropic-version": "2023-06-01"},
                              json={"model": "grok-chat-fast", "messages": [], "max_tokens": 1})
        assert message.status_code == 200 and message.json()["id"] == "msg_mock"
        image = client.post("/grok/v1/images/generations", headers=headers,
                            json={"model": "grok-imagine-image", "prompt": "synthetic", "n": 1})
        assert image.status_code == 200 and image.json()["data"][0]["b64_json"] == "c3ludGhldGlj"
        rejected = client.post("/grok/v1/images/generations", headers=headers,
                               json={"model": "grok-imagine-image", "prompt": "synthetic", "n": 2})
        assert rejected.status_code == 400
        edit = client.post("/grok/v1/images/edits", headers=headers,
                           data={"model": "grok-imagine-image-edit", "prompt": "synthetic", "n": "1"},
                           files={"image": ("synthetic.png", b"synthetic", "image/png")})
        assert edit.status_code == 200 and edit.json()["data"][0]["b64_json"] == "c3ludGhldGlj"
        video = client.post("/grok/v1/videos/generations", headers=headers, json={"model": "grok-imagine-video"})
        assert video.status_code == 200 and video.json()["id"] == "video_mock"
        assert client.get("/grok/v1/videos/video_mock", headers=headers).json()["status"] == "completed"
        speech = client.post("/grok/v1/audio/speech", headers=headers, json={"model": "grok-voice-latest"})
        assert speech.status_code == 200 and speech.content == b"synthetic-audio"
        transcript = client.post("/grok/v1/audio/transcriptions", headers=headers, files={"file": ("test.wav", b"audio")})
        assert transcript.status_code == 200 and transcript.json()["text"] == "synthetic transcript"
        upload = client.put("/grok/v1/media/uploads/" + "a" * 64, content=b"synthetic-video",
                            headers={"content-type": "video/mp4"})
        assert upload.status_code == 204
        assert client.get("/grok/v1/admin/accounts", headers=headers).status_code == 404

        ready = threading.Event()
        state: dict = {}

        async def echo(ws):
            assert ws.request.headers.get("authorization") == "Bearer synthetic-grok-key"
            async for value in ws:
                await ws.send(value)

        async def run_server():
            state["stop"] = asyncio.Event()
            state["loop"] = asyncio.get_running_loop()
            async with serve(echo, "127.0.0.1", 0) as server:
                state["port"] = server.sockets[0].getsockname()[1]
                ready.set()
                await state["stop"].wait()

        thread = threading.Thread(target=lambda: asyncio.run(run_server()), daemon=True)
        thread.start()
        if not ready.wait(10):
            raise RuntimeError("WebSocket stand-in did not start")
        try:
            os.environ["MEDIA2API_GROK_GATEWAY_URL"] = f"http://127.0.0.1:{state['port']}"
            for path in ("realtime", "stt"):
                with client.websocket_connect(f"/grok/v1/{path}?model=synthetic", headers=headers) as ws:
                    ws.send_text("synthetic-text")
                    assert ws.receive_text() == "synthetic-text"
                    ws.send_bytes(b"synthetic-audio")
                    assert ws.receive_bytes() == b"synthetic-audio"
        finally:
            state["loop"].call_soon_threadsafe(state["stop"].set)
            thread.join(timeout=10)
        assert not thread.is_alive()

    # Verify the actual Uvicorn access log, which is enabled in the Docker image.
    os.environ["MEDIA2API_GROK_GATEWAY_URL"] = base_url
    records = []

    class AccessCapture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    capture = AccessCapture()
    access_log = logging.getLogger("uvicorn.access")
    previous_level = access_log.level
    access_log.setLevel(logging.INFO)
    access_log.addHandler(capture)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=True))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        limit = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < limit:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("Uvicorn test server did not start")
        port = server.servers[0].sockets[0].getsockname()[1]
        ticket = "b" * 64
        with httpx.Client(trust_env=False) as client:
            result = client.put(f"http://127.0.0.1:{port}/grok/v1/media/uploads/{ticket}?sensitive=test",
                                content=b"synthetic-video", headers={"content-type": "video/mp4"})
        assert result.status_code == 204
        assert records and "REDACTED" in records[-1]
        assert ticket not in records[-1] and "sensitive" not in records[-1]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        access_log.removeHandler(capture)
        access_log.setLevel(previous_level)
    assert not thread.is_alive()
    print("Grok simulation: HTTP model/chat/image/video/audio and WebSocket echo checks passed")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
        return 0
    if len(sys.argv) != 2:
        print("usage: python scripts/run_grok_simulation_tests.py http://127.0.0.1:40103/grok")
        return 2
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="media2api-grok-simulation-") as directory:
        isolated = Path(directory)
        for name in ("api", "services", "utils"):
            shutil.copytree(root / name, isolated / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (isolated / "scripts").mkdir()
        shutil.copy2(Path(__file__), isolated / "scripts" / Path(__file__).name)
        shutil.copy2(root / "config.example.json", isolated / "config.json")
        shutil.copy2(root / "VERSION", isolated / "VERSION")
        env = dict(os.environ, MEDIA2API_AUTH_KEY="synthetic-local-key", STORAGE_BACKEND="json",
                   PYTHONPATH=str(isolated), PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(isolated / "scripts" / Path(__file__).name),
                               "--child", sys.argv[1]], cwd=isolated, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
