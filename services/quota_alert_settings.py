"""Validation shared by stored settings and the Feishu test endpoint."""
from __future__ import annotations

import re


DEFAULTS = {"enabled": False, "webhook_url": "", "secret": "", "threshold": 3, "repeat_minutes": 60}


def quota_alert_settings(value: object, existing: dict | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("飞书通知配置格式不正确")
    result = {**DEFAULTS, **(existing or {})}
    for key in ("enabled", "threshold", "repeat_minutes"):
        if key in value:
            result[key] = value[key]
    if type(result["enabled"]) is not bool:
        raise ValueError("启用通知必须是布尔值")
    for key, label, minimum, maximum in (("threshold", "剩余配额阈值", 0, 10000),
                                          ("repeat_minutes", "重复提醒间隔（分钟）", 15, 10080)):
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise ValueError(f"{label}必须是 {minimum} 至 {maximum} 的整数")
    for key in ("webhook_url", "secret"):
        incoming = value.get(key, "")
        if not isinstance(incoming, str):
            raise ValueError("Webhook 和签名密钥必须是文本")
        if incoming.strip():
            result[key] = incoming.strip()
    if value.get("clear_secret") is True:
        result["secret"] = ""
    if value.get("clear_webhook") is True:
        result["webhook_url"] = ""
    url = result["webhook_url"]
    if url and not re.fullmatch(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", url):
        raise ValueError("请输入有效的飞书自定义机器人 Webhook（open.feishu.cn）")
    if result["enabled"] and not url:
        raise ValueError("启用飞书通知前请填写 Webhook")
    if len(result["secret"]) > 512:
        raise ValueError("签名密钥过长")
    return result


def public_quota_alert_settings(settings: dict) -> dict:
    return {**settings, "webhook_url": "", "secret": "",
            "has_webhook": bool(settings["webhook_url"]), "has_secret": bool(settings["secret"])}
