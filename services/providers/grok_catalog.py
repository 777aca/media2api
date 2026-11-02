"""Grok Web catalog, ported from chenyme/grok2api v3.1.8 (MIT)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    id: str
    capability: str
    mode: str
    tier: int = 1


MODELS = {m.id: m for m in (
    Model("grok-chat-fast", "chat", "fast"),
    Model("grok-chat-auto", "chat", "auto", 2),
    Model("grok-chat-expert", "chat", "expert", 2),
    Model("grok-chat-heavy", "chat", "heavy", 3),
    Model("grok-imagine-image-lite", "image", "fast"),
    Model("grok-imagine-image", "image", "image_pro"),
    Model("grok-imagine-image-2.0", "image", "image_pro"),
    Model("grok-imagine-image-edit", "image_edit", "image_edit"),
    Model("grok-imagine-video", "video", "video"),
)}
TIERS = {"auto": 1, "basic": 1, "super": 2, "heavy": 3}


def is_grok(model: object) -> bool:
    return str(model or "").startswith(("grok-", "Web/grok-"))


def resolve(model: object) -> Model:
    from fastapi import HTTPException
    key = str(model or "").removeprefix("Web/")
    if key not in MODELS:
        raise HTTPException(400, "不支持的 Grok Web 模型")
    return MODELS[key]


def catalog(accounts: list[dict], capability: str = "") -> list[dict]:
    tier = max((TIERS.get(a.get("tier"), 1) for a in accounts
                if a.get("enabled", True) and a.get("status") != "reauth_required"), default=0)
    return [{"id": m.id, "object": "model", "created": 0, "owned_by": "grok",
             "permission": [], "root": m.id, "parent": None, "channels": ["grok_web"],
             "provider": "grok_web", "capabilities": [m.capability], "source": "grok_web",
             "minimum_tier": ("basic", "super", "heavy")[m.tier - 1]}
            for m in MODELS.values() if m.tier <= tier and (not capability or m.capability in capability.split(","))]
