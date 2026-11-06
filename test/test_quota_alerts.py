from __future__ import annotations

import base64
import hashlib
import hmac
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from services.quota_alert_settings import quota_alert_settings, public_quota_alert_settings
from services.quota_alerts import QuotaAlertService, QuotaAlertError, collect_low_quotas, send_feishu


WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-hook"


def account(remaining=2, **values):
    return {"pool_account_id": "synthetic-account", "email": "person@example.test",
            "status": "正常", "source_type": "chatgpt", "quota": remaining,
            "limits_progress": [{"feature_name": "image_gen", "remaining": 5}], **values}


class QuotaAlertTests(unittest.TestCase):
    def test_settings_validate_and_preserve_redacted_secrets(self):
        original = quota_alert_settings({"webhook_url": WEBHOOK, "secret": "private-secret"})
        public = public_quota_alert_settings(original)
        self.assertEqual(public["webhook_url"], "")
        self.assertEqual(public["secret"], "")
        self.assertTrue(public["has_webhook"])
        self.assertEqual(quota_alert_settings({**public, "threshold": 0}, original)["secret"], "private-secret")
        self.assertEqual(quota_alert_settings({"clear_secret": True}, original)["secret"], "")
        for update in ({"threshold": -1}, {"threshold": 1.5}, {"threshold": True},
                       {"repeat_minutes": 1}, {"enabled": True},
                       {"webhook_url": "https://open.feishu.cn.evil.test/hook/x"},
                       {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x?secret=y"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                quota_alert_settings(update)

    def test_only_known_active_quotas_alert_and_threshold_is_inclusive(self):
        items = collect_low_quotas([account(), account(pool_account_id="unknown", limits_progress=[]),
            account(pool_account_id="disabled", status="禁用"), account(pool_account_id="error", status="异常"),
            account(4, pool_account_id="ok"), account(-1, pool_account_id="negative")], [], 2, 1000)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["remaining"], 2)
        self.assertEqual(collect_low_quotas([account()], [], 0, 1000), [])
        self.assertEqual(collect_low_quotas([account(0)], [], 0, 1000)[0]["level"], "empty")

    def test_grok_ignores_unknown_expired_disabled_and_uses_percent_for_weekly(self):
        grok = {"id": "grok-id", "status": "active", "enabled": True, "tier": "super",
                "quotas": {"fast": {"remaining": 2, "reset_at": 2000},
                           "auto": {"remaining": None}, "image_edit": {"remaining": 0, "reset_at": 900},
                           "weekly": {"remaining": 250, "total": 10000, "reset_at": 2000}}}
        items = collect_low_quotas([], [grok], 3, 1000)
        self.assertEqual({item["dimension"] for item in items}, {"fast", "weekly"})
        self.assertEqual(next(item for item in items if item["dimension"] == "weekly")["remaining"], 2.5)
        self.assertEqual(collect_low_quotas([], [{**grok, "enabled": False}], 3, 1000), [])

    def test_delivery_signature_and_redacted_failures(self):
        settings = quota_alert_settings({"webhook_url": WEBHOOK, "secret": "private-secret"})
        with mock.patch("services.quota_alerts.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"code": 0}
            send_feishu(settings, "media2api 测试", now=1234)
            payload = post.call_args.kwargs["json"]
            expected = base64.b64encode(hmac.new(b"1234\nprivate-secret", b"", hashlib.sha256).digest()).decode()
            self.assertEqual(payload["sign"], expected)
            self.assertEqual(payload["timestamp"], "1234")
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
            for result in ({"code": 19024, "msg": "private-secret"}, {}, {"code": "0"}):
                post.return_value.json.return_value = result
                with self.assertRaises(QuotaAlertError) as failure:
                    send_feishu(settings, "test")
                self.assertNotIn("private-secret", str(failure.exception))
            post.side_effect = RuntimeError(WEBHOOK)
            with self.assertRaises(QuotaAlertError) as failure:
                send_feishu(settings, "test")
            self.assertNotIn("synthetic-hook", str(failure.exception))

    def test_dedup_restart_escalation_recovery_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = quota_alert_settings({"enabled": True, "webhook_url": WEBHOOK})
            accounts = [account()]
            sender = mock.Mock()
            def service():
                return QuotaAlertService(settings=lambda: settings, accounts=lambda: accounts,
                    grok_accounts=lambda: [], state_path=Path(directory) / "state.json", sender=sender)
            worker = service()
            worker.check_once(now=1000)
            sender.assert_called_once()
            self.assertNotIn("person@example.test", sender.call_args.args[1])
            service().check_once(now=1010)
            self.assertEqual(sender.call_count, 1)
            accounts[0]["quota"] = 0
            worker.check_once(now=1030)
            self.assertEqual(sender.call_count, 2)
            worker.check_once(now=1040)
            self.assertEqual(sender.call_count, 2)
            accounts[0]["quota"] = 10
            worker.check_once(now=1050)
            accounts[0]["quota"] = 1
            sender.side_effect = QuotaAlertError("发送失败")
            worker.check_once(now=1080)
            self.assertIsNotNone(worker.status()["last_error"])
            sender.side_effect = None
            worker.check_once(now=1090)
            self.assertEqual(sender.call_count, 3)
            worker.check_once(now=1390)
            self.assertEqual(sender.call_count, 4)
            worker.check_once(now=1400)
            self.assertEqual(sender.call_count, 4)
            worker.check_once(now=5000)
            self.assertEqual(sender.call_count, 5)
            settings["enabled"] = False
            worker.check_once(now=10000)
            self.assertEqual(sender.call_count, 5)

    def test_batches_do_not_drop_accounts_and_no_credentials_in_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            settings = quota_alert_settings({"enabled": True, "webhook_url": WEBHOOK, "secret": "private-secret"})
            accounts = [account(pool_account_id=f"id-{index}", access_token="private-token") for index in range(35)]
            sender = mock.Mock()
            worker = QuotaAlertService(settings=lambda: settings, accounts=lambda: accounts,
                grok_accounts=lambda: [], state_path=path, sender=sender)
            worker.check_once(now=1000)
            self.assertEqual(sender.call_args.args[1].count("配额不足"), 30)
            worker.check_once(now=1030)
            self.assertEqual(sender.call_args.args[1].count("配额不足"), 5)
            worker.check_once(now=1060)
            self.assertEqual(sender.call_count, 2)
            stored = path.read_text()
            for forbidden in ("private-", "person@", "synthetic-hook", "id-0"):
                self.assertNotIn(forbidden, stored)
            settings["webhook_url"] = WEBHOOK + "-changed"
            worker.check_once(now=1090)
            self.assertEqual(sender.call_count, 3)

    def test_new_alerts_are_not_starved_by_repeat_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = quota_alert_settings({"enabled": True, "webhook_url": WEBHOOK, "repeat_minutes": 15})
            accounts = [account(pool_account_id=f"id-{index}") for index in range(901)]
            sender = mock.Mock()
            worker = QuotaAlertService(settings=lambda: settings, accounts=lambda: accounts,
                grok_accounts=lambda: [], state_path=Path(directory) / "state.json", sender=sender)
            for index in range(31):
                worker.check_once(now=1000 + 30 * index)
            self.assertEqual(len(worker.sent), 901)

    def test_partial_recovery_rearms_exhaustion_before_repeat_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = quota_alert_settings({"enabled": True, "webhook_url": WEBHOOK})
            accounts = [account(0)]
            sender = mock.Mock()
            def service():
                return QuotaAlertService(settings=lambda: settings, accounts=lambda: accounts,
                    grok_accounts=lambda: [], state_path=Path(directory) / "state.json", sender=sender)
            worker = service()
            worker.check_once(now=1000)
            accounts[0]["quota"] = 2
            worker.check_once(now=1030)
            self.assertEqual(sender.call_count, 1)
            accounts[0]["quota"] = 0
            service().check_once(now=1060)
            self.assertEqual(sender.call_count, 2)


class QuotaAlertApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import api.system as system
        import api.support as support
        from services.config import ConfigStore
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "config.json"
        path.write_text('{"auth-key": "synthetic-admin-key"}', encoding="utf-8")
        self.config = ConfigStore(path)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(system, "config", self.config).start()
        mock.patch.object(support, "config", SimpleNamespace(auth_key="synthetic-admin-key")).start()
        mock.patch.object(support.auth_service, "authenticate", side_effect=lambda key:
            {"id": "user", "name": "User", "role": "user"} if key == "user-key" else None).start()
        self.sender = mock.patch.object(system, "send_feishu").start()
        service = mock.Mock()
        service.status.return_value = {"last_success_at": None, "last_error": None, "check_interval_seconds": 30}
        mock.patch.object(system, "get_quota_alert_service", return_value=service).start()
        app = FastAPI()
        app.include_router(system.create_router("test"))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer synthetic-admin-key"}

    def test_admin_only(self):
        for method, path in (("GET", "/api/quota-alerts"), ("POST", "/api/quota-alerts"), ("POST", "/api/quota-alerts/test")):
            for key, code in (("", 401), ("invalid", 401), ("user-key", 403)):
                response = self.client.request(method, path, json={}, headers={"Authorization": f"Bearer {key}"})
                self.assertEqual(response.status_code, code)
        self.sender.assert_not_called()

    def test_save_preserve_secrets_and_test_does_not_enable_or_save(self):
        draft = {"webhook_url": WEBHOOK, "secret": "private-secret", "threshold": 17, "enabled": False}
        response = self.client.post("/api/quota-alerts/test", json=draft, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config.get_quota_alert_settings()["webhook_url"], "")
        response = self.client.post("/api/quota-alerts", json=draft, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-secret", response.text)
        self.assertNotIn("synthetic-hook", response.text)
        public = response.json()["settings"]
        response = self.client.post("/api/quota-alerts", json={**public, "enabled": True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config.get_quota_alert_settings()["webhook_url"], WEBHOOK)
        self.assertEqual(self.config.get_quota_alert_settings()["threshold"], 17)
        response = self.client.get("/api/settings", headers=self.headers)
        self.assertNotIn("private-secret", response.text)
        self.assertNotIn("synthetic-hook", response.text)
        self.assertEqual(self.sender.call_count, 1)

    def test_validation_and_delivery_failure(self):
        response = self.client.post("/api/quota-alerts", json={"enabled": True}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.sender.side_effect = QuotaAlertError("飞书发送失败")
        response = self.client.post("/api/quota-alerts/test", json={"webhook_url": WEBHOOK}, headers=self.headers)
        self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
