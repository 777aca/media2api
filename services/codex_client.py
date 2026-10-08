"""Codex 目录与文本通道共用的客户端身份，不携带或记录账号凭据。"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from threading import Lock
from urllib.parse import urlsplit

from curl_cffi import requests


DEFAULT_CODEX_CLIENT_VERSION = "0.161.0"
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_RELEASES_URL = "https://api.github.com/repos/openai/codex/releases"
_CANONICAL_RELEASE_URL = "https://github.com/openai/codex/releases/latest"


def _version_tuple(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        return None
    return tuple(int(part) for part in value.split("."))


def _stable_version(payload: object) -> str | None:
    releases = payload if isinstance(payload, list) else [payload]
    versions: list[str] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
            continue
        tag = release.get("tag_name")
        if isinstance(tag, str) and tag.startswith("rust-v") and _version_tuple(tag[6:]) is not None:
            versions.append(tag[6:])
    return max(versions, key=lambda item: _version_tuple(item) or ()) if versions else None


def _version_from_release_url(value: object) -> str | None:
    if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    prefix = "/openai/codex/releases/tag/rust-v"
    if (
        parsed.scheme != "https" or parsed.netloc != "github.com"
        or parsed.query or parsed.fragment or not parsed.path.startswith(prefix)
    ):
        return None
    version = parsed.path[len(prefix):]
    parts = _version_tuple(version)
    if parts is None or version != ".".join(str(part) for part in parts):
        return None
    return version


def _fetch_official_version() -> str | None:
    # 版本查询使用项目的全局代理，不向 GitHub 发送 ChatGPT Authorization。
    from services.proxy_service import proxy_settings

    with requests.Session(**proxy_settings.build_session_kwargs(upstream=True, impersonate="chrome")) as session:
        for suffix in ("/latest", "?per_page=30"):
            try:
                response = session.get(
                    _RELEASES_URL + suffix,
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "media2api"},
                    timeout=8,
                )
                if response.status_code == 200:
                    version = _stable_version(response.json())
                    if version:
                        return version
            except Exception:
                continue
        # GitHub API 限流时，官方 latest 的重定向地址仍可提供稳定版标签。
        try:
            response = session.head(
                _CANONICAL_RELEASE_URL,
                headers={"Accept": "text/html", "User-Agent": "media2api"},
                timeout=8,
                allow_redirects=True,
            )
            if response.status_code == 200:
                return _version_from_release_url(response.url)
        except Exception:
            pass
    return None


class CodexClientVersion:
    """按需每六小时跟随官方稳定版；并发共用查询，失败和旧响应不降级。"""

    def __init__(self, *, fetch: Callable[[], str | None] = _fetch_official_version,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._version = DEFAULT_CODEX_CLIENT_VERSION
        self._expires_at = 0.0
        self._lock = Lock()

    def get(self) -> str:
        with self._lock:
            now = self._clock()
            if now >= self._expires_at:
                try:
                    latest = self._fetch()
                except Exception:
                    latest = None
                parsed = _version_tuple(latest)
                if parsed is not None and parsed > (_version_tuple(self._version) or ()):
                    self._version = str(latest)
                self._expires_at = self._clock() + (6 * 3600 if parsed is not None else 300)
            return self._version


_client_version = CodexClientVersion()


def get_codex_client_version() -> str:
    pinned = os.environ.get("MEDIA2API_CODEX_CLIENT_VERSION", "").strip()
    parsed = _version_tuple(pinned)
    if parsed is not None and parsed >= (0, 144, 0):
        return pinned
    return _client_version.get()


def codex_headers(access_token: str, account: Mapping[str, object] | None = None,
                  accept: str = "application/json") -> dict[str, str]:
    version = get_codex_client_version()
    headers = {
        "Authorization": f"Bearer {access_token}", "Accept": accept,
        "Content-Type": "application/json", "Originator": "codex_cli_rs",
        "User-Agent": f"codex_cli_rs/{version}", "Version": version,
    }
    account_id = (account or {}).get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id:
        try:
            segment = access_token.split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
            auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else None
            account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
        except (IndexError, ValueError, UnicodeDecodeError):
            account_id = None
    if isinstance(account_id, str) and account_id and not any(char in account_id for char in "\r\n"):
        headers["ChatGPT-Account-Id"] = account_id
    return headers
