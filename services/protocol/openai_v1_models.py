from __future__ import annotations

from datetime import datetime, timezone

from services.account_service import account_service
from utils.helper import CODEX_IMAGE_MODEL, all_web_image_models, register_discovered_web_image_models


def _image_models(*, include_source: bool) -> tuple[list[dict[str, object]], int]:
    """根据本地活跃账号生成图片入口，不查询网页或 Codex 文本目录。"""
    data: list[dict[str, object]] = []
    dynamic_models: set[str] = set()
    accounts = [
        account
        for account in account_service.list_accounts()
        if isinstance(account, dict) and account.get("access_token") and account.get("status") not in {"禁用", "异常"}
    ]
    codex_types = {
        normalized
        for account in accounts
        if account_service._normalize_source_type(account.get("source_type")) == "codex"
           and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if accounts:
        dynamic_models.add("gpt-image-2")
        dynamic_models.update(all_web_image_models())
    if codex_types & {"Plus", "Team", "Pro"}:
        dynamic_models.add(CODEX_IMAGE_MODEL)
    if "Plus" in codex_types:
        dynamic_models.add(f"plus-{CODEX_IMAGE_MODEL}")
    if "Team" in codex_types:
        dynamic_models.add(f"team-{CODEX_IMAGE_MODEL}")
    if "Pro" in codex_types:
        dynamic_models.add(f"pro-{CODEX_IMAGE_MODEL}")

    for model in sorted(dynamic_models):
        item: dict[str, object] = {
            "id": model,
            "object": "model",
            "created": 0,
            "owned_by": "media2api",
            "permission": [],
            "root": model,
            "parent": None,
        }
        if include_source:
            item["source"] = "compatibility"
            if model in all_web_image_models():
                item["entry_kind"] = "web_image"
        data.append(item)
    return data, len(accounts)


def list_models(*, force_refresh: bool = False) -> dict[str, object]:
    """OpenAI 兼容的图片目录；每次读取本地账号，保留刷新参数兼容调用方。"""
    data, _ = _image_models(include_source=False)
    from services.providers.grok_catalog import catalog
    from services.providers.grok_store import get_store
    data.extend(catalog(get_store().accounts(), "image,image_edit"))
    return {"object": "list", "data": data}


def list_all_models(*, force_refresh: bool = False) -> dict[str, object]:
    """Combine the local image entries with the cached per-account text catalog."""
    from services.model_service import model_catalog_service

    try:
        official = model_catalog_service.list_models(force_refresh=force_refresh)["data"]
        # 从上游官方目录发现 gpt-image-N.N-后缀 形式的网页图片型号，登记后图片目录和调用校验同时生效。
        register_discovered_web_image_models([item.get("id") for item in official if isinstance(item, dict)])
    except Exception:
        # The image catalog must remain usable when an upstream catalog fails.
        official = []
    images = list_models()["data"]
    by_id = {item["id"]: item for item in images}
    for item in official:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            by_id.setdefault(item["id"], item)
    from services.providers.grok_catalog import catalog
    from services.providers.grok_store import get_store
    for item in catalog(get_store().accounts()):
        by_id[item["id"]] = item
    return {"object": "list", "data": [by_id[key] for key in sorted(by_id)]}


def discover_upstream_image_models(*, force_refresh: bool = False) -> None:
    """读取上游官方目录，刷新成功后由 model_service 登记新发布的图片型号；失败不影响本地目录。"""
    from services.model_service import model_catalog_service
    try:
        model_catalog_service.list_models(force_refresh=force_refresh)
    except Exception:
        pass


def get_catalog(*, force_refresh: bool = False) -> dict[str, object]:
    """管理员图片目录；sync 表示本地目录刷新，不代表上游权限探测。"""
    data, account_count = _image_models(include_source=True)
    from services.providers.grok_catalog import catalog
    from services.providers.grok_store import get_store
    grok_accounts = get_store().accounts()
    data.extend(catalog(grok_accounts))
    account_count += len(grok_accounts)
    refreshed_at = datetime.now(timezone.utc).isoformat()
    return {
        "object": "list",
        "data": data,
        "sync": {
            "status": "success",
            "last_attempt_at": refreshed_at,
            "last_success_at": refreshed_at,
            "source_count": account_count,
            "successful_sources": account_count,
            "errors": [],
        },
    }
