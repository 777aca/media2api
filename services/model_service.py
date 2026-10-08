from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Condition, RLock
from typing import Any

from curl_cffi.requests.exceptions import Timeout

from services.account_service import AccountService, account_service
from services.openai_backend_api import InvalidModelResponseError, OpenAIBackendAPI
from utils.helper import UpstreamHTTPError
from utils.log import logger


@dataclass(frozen=True)
class ModelRoute:
    account_ids: frozenset[str]
    allow_anonymous: bool = False
    web_account_ids: frozenset[str] | None = None
    codex_account_ids: frozenset[str] = frozenset()

    def account_ids_for_channel(self, channel: str) -> frozenset[str]:
        if channel == "web":
            return self.account_ids if self.web_account_ids is None else self.web_account_ids
        if channel == "codex":
            return self.codex_account_ids
        raise ValueError("unsupported model channel")


class ModelUnavailableError(RuntimeError):
    pass


class ModelCatalogService:
    """按稳定账号标识缓存官方模型目录，不将套餐视作模型权限。"""

    def __init__(
        self,
        accounts: AccountService,
        *,
        backend_factory: Callable[..., Any] = OpenAIBackendAPI,
        cache_ttl_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._accounts = accounts
        self._backend_factory = backend_factory
        self._cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self._clock = clock
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._refreshing = False
        self._refresh_generation = 0
        self._expires_at = 0.0
        self._account_signature: tuple[tuple[str, ...], ...] = ()
        self._anonymous_models: dict[str, dict[str, Any]] = {}
        self._models_by_account_id: dict[str, dict[str, dict[str, Any]]] = {}
        self._codex_models_by_account_id: dict[str, dict[str, dict[str, Any]]] = {}
        self._sync: dict[str, Any] = {
            "status": "failed", "last_attempt_at": None, "last_success_at": None,
            "source_count": 0, "successful_sources": 0, "errors": [],
        }

    @staticmethod
    def _model_map(result: object) -> dict[str, dict[str, Any]]:
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise InvalidModelResponseError("invalid_response")
        models: dict[str, dict[str, Any]] = {}
        for item in result["data"]:
            if not isinstance(item, dict):
                raise InvalidModelResponseError("invalid_response")
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id or model_id != model_id.strip():
                raise InvalidModelResponseError("invalid_response")
            public_fields = {"id", "object", "created", "owned_by", "permission", "root", "parent", "display_name"}
            models.setdefault(model_id, {key: value for key, value in item.items() if key in public_fields})
        return models

    def _account_snapshot(self) -> tuple[dict[str, dict], tuple[tuple[str, ...], ...]]:
        active: dict[str, dict] = {}
        signature: list[tuple[str, ...]] = []
        for account in self._accounts.list_accounts():
            if not isinstance(account, dict):
                continue
            account_id = str(account.get("pool_account_id") or "")
            access_token = str(account.get("access_token") or "").strip()
            if not account_id or not access_token:
                continue
            status = str(account.get("status") or "正常")
            signature.append((
                account_id, hashlib.sha256(access_token.encode()).hexdigest(), status,
                str(account.get("type") or ""), str(account.get("source_type") or "web"),
                hashlib.sha256(str(account.get("proxy") or "").encode()).hexdigest(),
            ))
            if status not in {"禁用", "异常"}:
                active[account_id] = dict(account)
        return active, tuple(sorted(signature))

    def _fetch_models(self, access_token: str = "", *, channel: str = "web") -> dict[str, dict[str, Any]]:
        backend = self._backend_factory(access_token=access_token)
        try:
            result = backend.list_codex_models() if channel == "codex" else backend.list_models()
            return self._model_map(result)
        finally:
            backend.close()

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, InvalidModelResponseError):
            return "invalid_response"
        if isinstance(exc, UpstreamHTTPError):
            status = exc.status_code
            return f"upstream_http_{status}" if isinstance(status, int) and 100 <= status <= 599 else "upstream_unavailable"
        if isinstance(exc, Timeout):
            return "upstream_timeout"
        return "upstream_unavailable"

    def _prepare_account(self, account: dict) -> tuple[dict | None, str | None]:
        try:
            account_id = account["pool_account_id"]
            access_token = self._accounts.refresh_access_token(
                account["access_token"], event="model_catalog_sync",
            ) or account["access_token"]
            current = self._accounts.get_account(access_token)
            if (
                not current or current.get("pool_account_id") != account_id
                or current.get("status") in {"禁用", "异常"}
            ):
                return None, "account_unavailable"
            return current, None
        except Exception as exc:
            return None, self._error_code(exc)

    def _fetch_source(self, account: dict | None, channel: str = "web") -> tuple[dict[str, dict[str, Any]] | None, str | None]:
        try:
            if account is None:
                return self._fetch_models(), None
            current = self._accounts.get_account(account["access_token"])
            if (
                not current or current.get("pool_account_id") != account["pool_account_id"]
                or current.get("status") in {"禁用", "异常"}
            ):
                return None, "account_unavailable"
            return self._fetch_models(current["access_token"], channel=channel), None
        except Exception as exc:  # 外部异常原文可能携带凭据，仅公开固定错误码。
            return None, self._error_code(exc)

    def _refresh(self, active: dict[str, dict], signature: tuple[tuple[str, ...], ...]) -> None:
        outcomes: dict[tuple[str | None, str], tuple[dict[str, dict[str, Any]] | None, str | None]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(active) * 2 + 1)) as executor:
            futures = {(None, "web"): executor.submit(self._fetch_source, None)}
            preparations = {
                account_id: executor.submit(self._prepare_account, account)
                for account_id, account in active.items()
            }
            for account_id, preparation in preparations.items():
                account, code = preparation.result()
                for channel in ("web", "codex"):
                    if account is None:
                        outcomes[(account_id, channel)] = None, code
                    else:
                        futures[(account_id, channel)] = executor.submit(self._fetch_source, account, channel)
            for source, future in futures.items():
                outcomes[source] = future.result()

        # 请求过程中删除或禁用的账号不能被已开始的请求结果重新加入。
        current_active, current_signature = self._account_snapshot()
        with self._condition:
            self._models_by_account_id = {
                account_id: models for account_id, models in self._models_by_account_id.items()
                if account_id in current_active
            }
            self._codex_models_by_account_id = {
                account_id: models for account_id, models in self._codex_models_by_account_id.items()
                if account_id in current_active
            }
            errors: list[dict[str, str | None]] = []
            successful_sources = 0
            for (account_id, channel), (models, code) in outcomes.items():
                if models is not None:
                    successful_sources += 1
                    if account_id is None:
                        self._anonymous_models = models
                    elif account_id in current_active:
                        cache = self._codex_models_by_account_id if channel == "codex" else self._models_by_account_id
                        cache[account_id] = models
                else:
                    error = {
                        "account_id": account_id,
                        "source": "anonymous" if account_id is None else "account",
                        "channel": channel,
                        "code": code or "upstream_unavailable",
                    }
                    errors.append(error)
                    logger.warning({"event": "model_catalog_source_failed", **error})
            self._sync["source_count"] = len(outcomes)
            self._sync["successful_sources"] = successful_sources
            self._sync["errors"] = errors
            self._sync["status"] = "success" if not errors else "partial" if successful_sources else "failed"
            if not errors:
                self._sync["last_success_at"] = datetime.now(timezone.utc).isoformat()

            # 自身 Token 轮换保留缓存；其他账号/状态变化在下一次读取时立即同步。
            same_accounts = tuple(row[:1] + row[2:] for row in signature) == tuple(
                row[:1] + row[2:] for row in current_signature
            )
            self._account_signature = current_signature if same_accounts else signature
            self._expires_at = self._clock() + self._cache_ttl_seconds if same_accounts else 0.0

    def _ensure_catalog(self, *, force_refresh: bool = False) -> None:
        active, signature = self._account_snapshot()
        with self._condition:
            if self._refreshing:
                # 普通读取和强制同步均共享已经开始的同一批同步。
                generation = self._refresh_generation
                while self._refreshing and self._refresh_generation == generation:
                    self._condition.wait()
                return
            if not force_refresh and signature == self._account_signature and self._clock() < self._expires_at:
                return
            self._refreshing = True
            self._sync["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
            self._models_by_account_id = {
                account_id: models for account_id, models in self._models_by_account_id.items()
                if account_id in active
            }
            self._codex_models_by_account_id = {
                account_id: models for account_id, models in self._codex_models_by_account_id.items()
                if account_id in active
            }
        try:
            self._refresh(active, signature)
        finally:
            with self._condition:
                self._refresh_generation += 1
                self._refreshing = False
                self._condition.notify_all()

    def _union(self) -> dict[str, dict[str, Any]]:
        active, _signature = self._account_snapshot()
        with self._lock:
            union = {model_id: dict(item) for model_id, item in self._anonymous_models.items()}
            for account_id in sorted(active):
                for cache in (self._models_by_account_id, self._codex_models_by_account_id):
                    for model_id, item in cache.get(account_id, {}).items():
                        current = union.setdefault(model_id, dict(item))
                        if not current.get("display_name") and item.get("display_name"):
                            current["display_name"] = item["display_name"]
            return union

    def list_models(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self._ensure_catalog(force_refresh=force_refresh)
        union = self._union()
        return {"object": "list", "data": [union[model_id] for model_id in sorted(union)]}

    def get_catalog(self, *, force_refresh: bool = False) -> dict[str, Any]:
        result = self.list_models(force_refresh=force_refresh)
        active, _signature = self._account_snapshot()
        with self._lock:
            sync = {**self._sync, "errors": [dict(error) for error in self._sync["errors"]]}
            data = []
            for model in result["data"]:
                model_id = model["id"]
                channels = []
                if model_id in self._anonymous_models or any(
                    model_id in self._models_by_account_id.get(account_id, {}) for account_id in active
                ):
                    channels.append("web")
                if any(model_id in self._codex_models_by_account_id.get(account_id, {}) for account_id in active):
                    channels.append("codex")
                data.append({**model, "source": "official", "channels": channels})
        return {
            **result, "data": data,
            "sync": sync,
        }

    def route_for_model(self, model: str) -> ModelRoute:
        model = str(model or "").strip()
        self._ensure_catalog()
        active, _signature = self._account_snapshot()
        with self._lock:
            web_ids = frozenset(
                    account_id for account_id, models in self._models_by_account_id.items()
                    if account_id in active and model in models
            )
            codex_ids = frozenset(
                account_id for account_id, models in self._codex_models_by_account_id.items()
                if account_id in active and model in models
            )
            return ModelRoute(
                account_ids=web_ids | codex_ids,
                web_account_ids=web_ids,
                codex_account_ids=codex_ids,
                allow_anonymous=model in self._anonymous_models,
            )


model_catalog_service = ModelCatalogService(account_service)
