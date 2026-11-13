from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from services.account_credentials import normalize_credentials, totp_code
from services.account_service import AccountService


SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 公开测试向量


def response(data: dict | None = None, *, status: int = 200, location: str = "") -> SimpleNamespace:
    return SimpleNamespace(status_code=status, text="{}", json=lambda: data or {},
                           url="https://auth.openai.com/log-in/password", headers={"location": location})


class AccountCredentialLoginTests(unittest.TestCase):
    def test_totp_matches_rfc_vectors_including_zero_padding(self) -> None:
        for timestamp, expected in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (20000000000, "353130")):
            self.assertEqual(totp_code(SECRET.lower(), timestamp=timestamp), expected)

    def test_normalizes_credentials_and_rejects_invalid_base32(self) -> None:
        item = {"email": " test@example.test ", "password": " -password ", "totp_secret": SECRET.lower()}
        self.assertEqual(normalize_credentials(item), {"email": "test@example.test", "password": "-password", "totp_secret": SECRET})
        for secret in ("invalid-secret", "A" * 17, "", None):
            with self.assertRaises(ValueError):
                normalize_credentials({**item, "totp_secret": secret})

    def run_login(self, password_response: SimpleNamespace, *, mfa_response: SimpleNamespace | None = None,
                  secret: str = SECRET, redirect: bool = False) -> tuple[dict, mock.MagicMock]:
        session = mock.MagicMock()
        session.get.side_effect = [response(), *([response(status=302, location="https://platform.openai.com/auth/callback?code=synthetic-code")] if redirect else []), response()]
        session.post.side_effect = [password_response, *([mfa_response] if mfa_response is not None else []),
                                    response({"access_token": "synthetic-token", "refresh_token": "synthetic-refresh"})]
        with mock.patch("curl_cffi.requests.Session", return_value=session), \
             mock.patch("utils.sentinel.build_sentinel_token", return_value=("synthetic-sentinel", "")), \
             mock.patch("services.account_service.config.get_proxy_settings", return_value=""), \
             mock.patch("services.account_credentials.time.time", return_value=59):
            result = AccountService._login_with_password(AccountService.__new__(AccountService), "test@example.test", "synthetic-password", secret)
        session.close.assert_called_once()
        return result, session

    def test_password_and_totp_challenge_exchange_real_token_fields(self) -> None:
        result, session = self.run_login(
            response({"page": {"type": "mfa_challenge"}, "continue_url": "/mfa-challenge/synthetic-challenge"}),
            mfa_response=response({"continue_url": "/oauth/resume"}), redirect=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "synthetic-token")
        self.assertEqual(session.post.call_args_list[1].args[0], "https://auth.openai.com/api/accounts/mfa/verify")
        self.assertEqual(session.post.call_args_list[1].kwargs["json"], {"code": "287082", "type": "totp", "id": "synthetic-challenge"})
        self.assertEqual(session.post.call_args_list[2].kwargs["json"]["code"], "synthetic-code")
        self.assertNotIn(SECRET, str(session.post.call_args_list))

    def test_mfa_failure_does_not_exchange_a_token(self) -> None:
        result, session = self.run_login(
            response({"continue_url": "/mfa-challenge/synthetic-challenge"}), mfa_response=response(status=403),
        )
        self.assertEqual(result, {"ok": False, "error": "mfa_verify_failed"})
        self.assertEqual(session.post.call_count, 2)

    def test_password_only_login_remains_compatible(self) -> None:
        result, session = self.run_login(response({"continue_url": "https://platform.openai.com/auth/callback?code=synthetic-code"}), secret="")
        self.assertTrue(result["ok"])
        self.assertEqual(session.post.call_count, 2)

    def test_email_otp_remains_a_distinct_failure(self) -> None:
        result, session = self.run_login(response({"page": {"type": "email_otp_verification"}, "continue_url": "/email-verification"}))
        self.assertEqual(result["error"], "need_verification_code")
        self.assertEqual(session.post.call_count, 1)

    def test_external_continuation_is_never_followed_or_used_as_a_code(self) -> None:
        result, session = self.run_login(response({"continue_url": "https://untrusted.example/callback?code=synthetic-code"}))
        self.assertEqual(result["error"], "no_auth_code")
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
