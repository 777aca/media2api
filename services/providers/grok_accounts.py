from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager

from fastapi import HTTPException

from services.providers.grok_catalog import TIERS, resolve
from services.providers.grok_store import get_store


def parse_import(value: str | list | dict) -> list[dict]:
    if isinstance(value, str):
        text = value.lstrip("\ufeff").strip()
        if text.startswith(("{", "[")):
            try:
                value = json.loads(text)
            except ValueError as exc:
                raise HTTPException(400, "Grok 账号 JSON 无效") from exc
        else:
            value = [{"sso_token": token} for token in text.splitlines() if token.strip()]
    if isinstance(value, dict):
        if value.get("provider", "grok_web") != "grok_web":
            raise HTTPException(400, "仅支持 Grok Web 账号")
        value = value.get("accounts", [value])
    if not isinstance(value, list) or not 1 <= len(value) <= 10000:
        raise HTTPException(400, "请输入 1–10000 个 Grok Web 账号")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise HTTPException(400, "Grok 账号格式无效")
        token = str(item.get("sso_token") or item.get("token") or "").strip()
        if token.lower().startswith("sso="):
            token = token[4:]
        token = token.split(";", 1)[0].strip()
        cookies = str(item.get("cloudflare_cookies") or "")
        if not token or len(token) > 16384 or any(ord(c) < 33 for c in token) or "\n" in cookies or "\r" in cookies:
            raise HTTPException(400, "SSO 或 Cookie 格式无效")
        tier = item.get("tier") or "auto"
        if tier not in TIERS:
            raise HTTPException(400, "账号等级必须是 auto/basic/super/heavy")
        if token not in seen:
            result.append({**{k: str(item.get(k) or "")[:255] for k in ("name", "email", "user_id")},
                           "sso_token": token, "cloudflare_cookies": cookies, "tier": tier})
            seen.add(token)
    return result


class GrokAccountPool:
    def __init__(self):
        self.lock = threading.RLock()
        self.inflight: dict[str, int] = {}
        self.users: dict[str, int] = {}

    def try_acquire_image_token(self, *, model, channel="grok_web", excluded_ids=None, preferred_id=""):
        return self.acquire(model, preferred_id=preferred_id, excluded_ids=excluded_ids or set(), required=False)

    def acquire(self, model, *, preferred_id="", excluded_ids=None, required=True):
        spec = resolve(model)
        store = get_store()
        settings = store.settings()
        with self.lock:
            candidates = [a for a in store.accounts() if a.get("enabled", True)
                and a.get("status") != "reauth_required" and a.get("cooldown_until", 0) <= time.time()
                and a["id"] not in (excluded_ids or set()) and (not preferred_id or a["id"] == preferred_id)
                and TIERS.get(a.get("tier"), 1) >= spec.tier
                and self.inflight.get(a["id"], 0) < settings["account_concurrency"]]
            candidates.sort(key=lambda a: (self.inflight.get(a["id"], 0), a.get("last_used", 0)))
            for a in candidates:
                quotas = a.get("quotas", {})
                mode = "video720p" if spec.capability == "video" else spec.mode
                windows = [quotas.get(mode, {}), quotas.get("weekly", {})]
                if any(q.get("remaining", 1) <= 0 and q.get("reset_at", 0) > time.time() for q in windows):
                    continue
                self.inflight[a["id"]] = self.inflight.get(a["id"], 0) + 1
                store.update_account(a["id"], {"last_used": time.time()})
                return a["id"]
        if required:
            raise HTTPException(429, "没有可用的 Grok Web 账号或账号并发已满")
        return None

    def release_image_slot(self, account_id):
        with self.lock:
            self.inflight[account_id] = max(0, self.inflight.get(account_id, 1) - 1)

    def get_account(self, account_id):
        return {**get_store().account(account_id), "pool_account_id": account_id}

    @contextmanager
    def lease(self, model, owner, preferred_id=""):
        settings = get_store().settings()
        with self.lock:
            if sum(self.users.values()) >= settings["global_concurrency"] or self.users.get(owner, 0) >= settings["user_concurrency"]:
                raise HTTPException(429, "Grok 请求并发已满")
            self.users[owner] = self.users.get(owner, 0) + 1
        account_id = None
        try:
            account_id = self.acquire(model, preferred_id=preferred_id)
            yield get_store().account(account_id, secret=True)
        finally:
            if account_id:
                self.release_image_slot(account_id)
            with self.lock:
                self.users[owner] = max(0, self.users.get(owner, 1) - 1)


pool = GrokAccountPool()
