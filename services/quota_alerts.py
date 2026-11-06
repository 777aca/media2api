"""Local quota snapshot watcher; never refreshes provider credentials or quota itself."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curl_cffi import requests


class QuotaAlertError(RuntimeError):
    pass


def send_feishu(settings: dict, message: str, *, now: float | None = None) -> None:
    if not settings.get("webhook_url"):
        raise QuotaAlertError("请先填写飞书 Webhook")
    payload = {"msg_type": "text", "content": {"text": message}}
    if settings.get("secret"):
        timestamp = str(int(time.time() if now is None else now))
        key = f"{timestamp}\n{settings['secret']}".encode()
        payload.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
    try:
        response = requests.post(settings["webhook_url"], json=payload, timeout=10, allow_redirects=False)
        try:
            if response.status_code != 200:
                raise QuotaAlertError("飞书 HTTP 请求失败，请检查网络与机器人配置")
            body = response.json()
            if not isinstance(body, dict) or type(body.get("code")) is not int or body["code"] != 0:
                raise QuotaAlertError("飞书未确认接收，请检查机器人关键词、签名或 IP 白名单")
        finally:
            response.close()
    except QuotaAlertError:
        raise
    except Exception:
        # Transport errors may include the credential-bearing webhook URL.
        raise QuotaAlertError("飞书发送失败，请检查网络与机器人配置") from None


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _expired(value: object, now: float) -> bool:
    if _number(value):
        return value > 0 and value <= now
    if isinstance(value, str) and value:
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return date.timestamp() <= now
        except (ValueError, OverflowError, OSError):
            pass
    return False


def collect_low_quotas(accounts: list[dict], grok_accounts: list[dict], threshold: int, now: float) -> list[dict]:
    groups = {}

    def add(provider, account_id, dimension, remaining, label, total=None):
        if not account_id or not _number(remaining):
            return
        key = f"pool-v2:{provider}:{dimension}"
        group = groups.setdefault(key, {"key": key, "dimension": dimension, "label": f"{provider} · {label}号池",
            "remaining": 0, "total": 0, "members": set(), "unit": "%" if total is not None else "次"})
        if account_id in group["members"]:
            return
        group["members"].add(account_id)
        group["remaining"] += remaining
        if total is not None:
            group["total"] += total

    for account in accounts:
        if account.get("status") not in {"正常", "限流"}:
            continue
        # Normalization historically maps unknown quota to zero. Require upstream evidence.
        limits = account.get("limits_progress")
        known = isinstance(limits, list) and any(isinstance(item, dict) and item.get("feature_name") == "image_gen"
            and _number(item.get("remaining")) for item in limits)
        if known and not _expired(account.get("restore_at"), now):
            add("ChatGPT", account.get("pool_account_id"), "image_gen", account.get("quota"), "图片")
    labels = {"fast": "快速", "auto": "自动", "expert": "专家", "heavy": "Heavy",
              "image_pro": "图片", "image_edit": "图片编辑", "video": "视频", "video720p": "720p 视频", "weekly": "周额度"}
    for account in grok_accounts:
        if not account.get("enabled", True) or account.get("status") != "active":
            continue
        quotas = account.get("quotas")
        if not isinstance(quotas, dict):
            continue
        for dimension, label in labels.items():
            quota = quotas.get(dimension)
            if not isinstance(quota, dict) or _expired(quota.get("reset_at"), now):
                continue
            remaining = quota.get("remaining")
            total = None
            if dimension == "weekly":
                total = quota.get("total")
                if not _number(remaining) or not _number(total) or total <= 0:
                    continue
            add("Grok", account.get("id"), dimension, remaining, label, total)
    entries = []
    for group in groups.values():
        remaining = 100 * group["remaining"] / group["total"] if group["unit"] == "%" else group["remaining"]
        # Compare only after summing every eligible account, including healthy ones.
        if remaining <= threshold:
            entries.append({"key": group["key"], "dimension": group["dimension"], "label": group["label"],
                "remaining": remaining, "unit": group["unit"], "account_count": len(group["members"]),
                "level": "empty" if group["remaining"] == 0 else "low"})
    return entries


def _message(entries: list[dict], now: float) -> str:
    when = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    lines = ["media2api 号池总配额提醒", when]
    for item in entries:
        state = "已知额度已耗尽" if item["level"] == "empty" else "配额不足"
        lines.append(f"{item['label']}：{state}，已知总剩余 {item['remaining']:g}{item['unit']}（汇总 {item['account_count']} 个账号）")
    lines.append("同类额度跨账号汇总；未知或过期配额未计入。数据来自本地最新快照，请在号池管理中查看或刷新。")
    return "\n".join(lines)


class QuotaAlertService:
    def __init__(self, *, settings, accounts, grok_accounts, state_path: Path, sender=send_feishu):
        self.settings = settings
        self.accounts = accounts
        self.grok_accounts = grok_accounts
        self.state_path = state_path
        self.sender = sender
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.retry_after = 0
        self.last_error = None
        self.last_success_at = None
        self.destination = ""
        self.sent = {}
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.destination = str(state.get("destination", ""))
            self.sent = {key: value for key, value in state.get("sent", {}).items()
                         if isinstance(value, dict) and _number(value.get("at")) and value.get("level") in {"low", "empty"}}
            if _number(state.get("last_success_at")):
                self.last_success_at = state["last_success_at"]
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def status(self) -> dict:
        with self.lock:
            return {"last_success_at": self.last_success_at, "last_error": self.last_error,
                    "check_interval_seconds": 30}

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"destination": self.destination, "sent": self.sent,
            "last_success_at": self.last_success_at}), encoding="utf-8")
        temporary.replace(self.state_path)

    def check_once(self, *, now: float | None = None):
        now = time.time() if now is None else now
        with self.lock:
            settings = self.settings()
            if not settings["enabled"]:
                return
            destination = hashlib.sha256(f"{settings['webhook_url']}\n{settings['secret']}".encode()).hexdigest()
            if destination != self.destination:
                self.destination, self.sent, self.retry_after = destination, {}, 0
            entries = collect_low_quotas(self.accounts(), self.grok_accounts(), settings["threshold"], now)
            current = {item["key"] for item in entries}
            previous_count = len(self.sent)
            self.sent = {key: value for key, value in self.sent.items() if key in current}
            changed = len(self.sent) != previous_count
            for item in entries:
                previous = self.sent.get(item["key"])
                if previous and item["level"] == "low" and previous["level"] == "empty":
                    # Even a partial recovery rearms exhaustion; retain the repeat timestamp.
                    previous["level"] = "low"
                    changed = True
            if changed:
                self._save()
            if now < self.retry_after:
                return
            pending = [item for item in entries if item["key"] not in self.sent
                or (item["level"] == "empty" and self.sent[item["key"]]["level"] != "empty")
                or now - self.sent[item["key"]]["at"] >= settings["repeat_minutes"] * 60]
            pending.sort(key=lambda item: (
                0 if item["key"] not in self.sent or item["level"] != self.sent[item["key"]]["level"] else 1,
                self.sent.get(item["key"], {}).get("at", 0),
            ))
            # Bound message size and bot rate: at most one batch per 30-second scan.
            pending = pending[:30]
            if not pending:
                return
            try:
                self.sender(settings, _message(pending, now))
            except Exception:
                self.last_error = "飞书推送失败，请检查网络、关键词、签名或 IP 白名单；5 分钟后重试"
                self.retry_after = now + 300
                return
            self.sent.update({item["key"]: {"level": item["level"], "at": now} for item in pending})
            self.last_success_at, self.last_error = now, None
            self._save()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def run():
            while not self.stop_event.is_set():
                try:
                    self.check_once()
                except Exception:
                    with self.lock:
                        self.last_error = "配额检查失败，请检查本地配置与通知状态文件"
                self.stop_event.wait(30)
        self.thread = threading.Thread(target=run, name="quota-alerts", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=12)


def _default_service():
    from services.config import DATA_DIR, config
    from services.account_service import account_service
    from services.providers.grok_store import get_store
    return QuotaAlertService(settings=config.get_quota_alert_settings, accounts=account_service.list_accounts,
        grok_accounts=lambda: get_store().accounts(), state_path=DATA_DIR / "quota-alert-state.json")


# Deferred singleton avoids initializing provider storage merely by importing this module.
_service = None


def get_quota_alert_service() -> QuotaAlertService:
    global _service
    if _service is None:
        _service = _default_service()
    return _service
