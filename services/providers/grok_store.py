"""Local native-provider state, isolated from legacy ChatGPT account snapshots."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
import re

from cryptography.fernet import Fernet
from fastapi import HTTPException

DEFAULTS = {"proxy_url": "", "signer_url": "https://grok.wodf.de/sign", "statsig_value": "",
            "chat_timeout": 120, "image_timeout": 180, "video_timeout": 900,
            "global_concurrency": 8, "user_concurrency": 4, "account_concurrency": 2,
            "free_video_duration_cap": 6, "retention_days": 30}


class GrokStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / "native-providers.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS grok_accounts(id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, metadata TEXT, secret TEXT);
            CREATE TABLE IF NOT EXISTS provider_state(kind TEXT, id TEXT, owner TEXT, updated REAL, value TEXT, PRIMARY KEY(kind,id));
            CREATE INDEX IF NOT EXISTS provider_state_owner ON provider_state(kind,owner,updated);
        """)
        self.db.commit()
        self._cipher = None

    def cipher(self):
        with self.lock:
            if self._cipher is None:
                path = self.directory / ".native-provider-key"
                if not path.exists():
                    with open(path, "xb") as f:
                        f.write(Fernet.generate_key())
                    os.chmod(path, 0o600)
                self._cipher = Fernet(path.read_bytes().strip())
            return self._cipher

    def accounts(self) -> list[dict]:
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT metadata FROM grok_accounts ORDER BY id")]

    def account(self, account_id: str, *, secret=False) -> dict:
        with self.lock:
            row = self.db.execute("SELECT metadata,secret FROM grok_accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Grok 账号不存在")
            result = json.loads(row[0])
            if secret:
                result.update(json.loads(self.cipher().decrypt(row[1].encode())))
            return result

    def import_accounts(self, entries: list[dict]) -> int:
        import hashlib
        with self.lock, self.db:
            for entry in entries:
                fingerprint = hashlib.sha256(entry["sso_token"].encode()).hexdigest()
                row = self.db.execute("SELECT id,metadata FROM grok_accounts WHERE fingerprint=?", (fingerprint,)).fetchone()
                metadata = json.loads(row[1]) if row else {"id": uuid.uuid4().hex, "provider": "grok_web",
                    "enabled": True, "status": "active", "tier": "auto", "created_at": time.time()}
                metadata.update({k: entry[k] for k in ("name", "tier", "email", "user_id") if entry.get(k)})
                metadata.setdefault("name", "Grok Web " + fingerprint[:8])
                metadata["status"] = "active"
                secrets = {k: entry.get(k, "") for k in ("sso_token", "cloudflare_cookies")}
                encrypted = self.cipher().encrypt(json.dumps(secrets).encode()).decode()
                self.db.execute("INSERT OR REPLACE INTO grok_accounts VALUES (?,?,?,?)",
                                (metadata["id"], fingerprint, json.dumps(metadata), encrypted))
        return len(entries)

    def update_account(self, account_id: str, values: dict):
        with self.lock, self.db:
            metadata = self.account(account_id)
            metadata.update(values)
            self.db.execute("UPDATE grok_accounts SET metadata=? WHERE id=?", (json.dumps(metadata), account_id))
            return metadata

    def delete_account(self, account_id: str):
        with self.lock, self.db:
            self.db.execute("DELETE FROM grok_accounts WHERE id=?", (account_id,))

    def put(self, kind: str, key: str, owner: str, value: dict):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO provider_state VALUES (?,?,?,?,?)",
                            (kind, key, owner, time.time(), json.dumps(value, ensure_ascii=False)))

    def get(self, kind: str, key: str, owner: str | None = None) -> dict:
        with self.lock:
            row = self.db.execute("SELECT owner,value FROM provider_state WHERE kind=? AND id=?", (kind, key)).fetchone()
            if not row or (owner is not None and row[0] != owner):
                raise HTTPException(404, "记录不存在或不属于当前用户")
            return json.loads(row[1])

    def list(self, kind: str, owner: str | None = None, *, limit: int | None = 500) -> list[dict]:
        with self.lock:
            query = "SELECT value FROM provider_state WHERE kind=?"
            params = [kind]
            if owner is not None:
                query += " AND owner=?"
                params.append(owner)
            query += " ORDER BY updated DESC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            return [json.loads(r[0]) for r in self.db.execute(query, params)]

    def video_by_key(self, owner: str, key: str):
        with self.lock:
            row = self.db.execute("SELECT value FROM provider_state WHERE kind='video' AND owner=? AND json_extract(value,'$.idempotency_key')=? LIMIT 1", (owner,key)).fetchone()
            return json.loads(row[0]) if row else None

    def delete(self, kind: str, key: str, owner: str):
        with self.lock, self.db:
            self.get(kind, key, owner)
            self.db.execute("DELETE FROM provider_state WHERE kind=? AND id=? AND owner=?", (kind, key, owner))

    def settings(self) -> dict:
        try:
            item = self.get("settings", "grok", "admin")
        except HTTPException:
            return dict(DEFAULTS)
        return {**DEFAULTS, **json.loads(self.cipher().decrypt(item["encrypted"].encode()))}

    def save_settings(self, values: dict):
        settings = self.settings()
        for key, value in values.items():
            if key not in DEFAULTS:
                raise HTTPException(400, "未知 Grok 设置")
            if isinstance(DEFAULTS[key], int):
                maximum = 15 if key == "free_video_duration_cap" else 128 if key.endswith("concurrency") else 3650 if key == "retention_days" else 3600
                if type(value) is not int or not 1 <= value <= maximum:
                    raise HTTPException(400, f"{key} 必须是 1–{maximum} 的整数")
            elif not isinstance(value, str) or len(value) > 16384 or "\n" in value or "\r" in value:
                raise HTTPException(400, "Grok 设置格式无效")
            settings[key] = value
        self.put("settings", "grok", "admin", {"encrypted": self.cipher().encrypt(json.dumps(settings).encode()).decode()})

    def cleanup(self):
        """Expire inactive histories and finished media; never remove pending jobs."""
        cutoff = time.time() - self.settings()["retention_days"] * 86400
        with self.lock, self.db:
            rows = self.db.execute("SELECT kind,id,value FROM provider_state WHERE updated<? AND kind IN ('response','conversation','video')", (cutoff,)).fetchall()
            for kind, key, raw in rows:
                value = json.loads(raw)
                if kind == "video":
                    if value.get("status") not in {"completed", "failed", "uncertain"}:
                        continue
                    if re.fullmatch(r"video_[0-9a-f]{32}", key):
                        directory = (self.directory / "videos").resolve()
                        for suffix in (".mp4", ".part"):
                            path = directory / (key + suffix)
                            if path.resolve().parent == directory:
                                path.unlink(missing_ok=True)
                self.db.execute("DELETE FROM provider_state WHERE kind=? AND id=?", (kind, key))


_store = None
_lock = threading.Lock()


def get_store() -> GrokStore:
    global _store
    with _lock:
        if _store is None:
            from services.config import DATA_DIR
            _store = GrokStore(DATA_DIR)
        return _store
