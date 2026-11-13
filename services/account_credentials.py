"""三段式账号凭据校验与 RFC 6238 动态码，不记录原始凭据。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time


def normalize_credentials(item: dict) -> dict[str, str]:
    fields = {key: item.get(key) for key in ("email", "password", "totp_secret")}
    if not all(isinstance(value, str) for value in fields.values()):
        raise ValueError("邮箱、密码和二步验证密钥均不能为空")
    email, password, secret = (fields[key].strip() for key in fields)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("邮箱格式不正确")
    if not password:
        raise ValueError("密码不能为空")
    if not re.fullmatch(r"[A-Z2-7]{16,}={0,6}", secret, re.IGNORECASE):
        raise ValueError("二步验证密钥应为至少 16 位的 Base32 字符串")
    try:
        unpadded = secret.rstrip("=").upper()
        base64.b32decode(unpadded + "=" * (-len(unpadded) % 8))
    except ValueError:
        raise ValueError("二步验证密钥不是有效的 Base32 字符串") from None
    return {"email": email, "password": password, "totp_secret": unpadded}


def totp_code(secret: str, *, timestamp: float | None = None) -> str:
    normalized = secret.strip().rstrip("=").upper()
    key = base64.b32decode(normalized + "=" * (-len(normalized) % 8))
    counter = int(time.time() if timestamp is None else timestamp) // 30
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


def credential_login_error(code: object) -> str:
    return {
        "invalid_password": "密码错误",
        "mfa_verify_failed": "二步验证失败，请检查密钥和服务器时间",
        "mfa_challenge_missing": "上游未返回二步验证挑战标识",
        "need_verification_code": "上游要求邮箱验证码，请使用浏览器 OAuth 登录",
        "rate_limit_exceeded": "上游登录限流，请稍后重试",
        "unsupported_country_region_territory": "当前代理地区不支持登录",
        "token_exchange_failed": "登录授权码兑换 Token 失败",
    }.get(str(code), "自动登录失败，请检查账号或使用浏览器 OAuth 登录")
