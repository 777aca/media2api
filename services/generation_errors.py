"""Image-only error policy. Submission certainty is separate from retryability."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time


class GenerationRuntimeError(RuntimeError):
    def __init__(self, message: str, code: str, status_code: int = 429):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.error_type = "invalid_request_error" if status_code < 500 else "server_error"

    def to_openai_error(self) -> dict:
        return {"error": {"message": str(self), "code": self.code, "type": self.error_type, "param": None}}


class RetryImage(Exception):
    """A safely rejected attempt must rejoin the durable queue before changing accounts."""


@dataclass(frozen=True)
class ImageFailure:
    category: str
    retryable: bool = False
    cooldown_seconds: int = 0
    scope: str = "channel"
    explicit_rejection: bool = False


def classify_image_error(exc: Exception, consecutive: int = 0, *, now: float | None = None) -> ImageFailure:
    now = time.time() if now is None else now
    status = getattr(exc, "upstream_status", None) or getattr(exc, "status_code", None)
    body = getattr(exc, "body", None) or getattr(exc, "detail", {})
    error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else body
    error = error if isinstance(error, dict) else {}
    code = str(error.get("code") or error.get("type") or getattr(exc, "code", "") or "").lower()
    text = str(exc).lower()
    if code.startswith("image_quota") or code in {"image_queue_full", "image_queue_timeout", "image_key_disabled", "image_account_busy", "idempotency_conflict", "image_task_ended", "image_task_cancelled"}:
        return ImageFailure("local_rejection", explicit_rejection=True)
    if code == "no_image_generated":
        return ImageFailure("platform_error")
    # A structured semantic error is more precise than a generic HTTP status.
    if code in {"content_policy_violation", "moderation_blocked", "content_filter", "safety_violation"} or type(exc).__name__ == "ImageContentPolicyError":
        return ImageFailure("content_rejected", explicit_rejection=True)
    if code in {"model_permission_denied", "model_not_found", "model_not_available", "unsupported_model", "permission_denied_for_model"}:
        return ImageFailure("model_permission", True, scope="model", explicit_rejection=True)
    if status == 401 or type(exc).__name__ == "InvalidAccessTokenError" or code in {"token_invalidated", "invalid_api_key", "invalid_token", "token_expired", "session_expired"}:
        return ImageFailure("credentials_invalid", True, scope="account", explicit_rejection=True)
    if status == 429 or code in {"rate_limit_exceeded", "insufficient_quota", "usage_limit_reached", "upstream_rate_limit", "quota_exceeded"}:
        delay = 180.0
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is None:
            retry_after = error.get("retry_after", error.get("reset_after"))
        try:
            if retry_after is not None:
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = parsedate_to_datetime(str(retry_after)).timestamp() - now
            elif error.get("reset_at") or error.get("reset_time"):
                value = error.get("reset_at") or error.get("reset_time")
                try:
                    reset = float(value)
                except (TypeError, ValueError):
                    reset_date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    reset = (reset_date if reset_date.tzinfo else reset_date.replace(tzinfo=timezone.utc)).timestamp()
                delay = reset - now
        except (TypeError, ValueError, OverflowError):
            delay = 180
        return ImageFailure("rate_limited", True, int(max(60, min(604800, delay))), explicit_rejection=True)
    if status == 403:
        return ImageFailure("access_denied", True, 300, explicit_rejection=True)
    if status in {400, 404, 405, 413, 415, 422} or code in {"invalid_image_request", "invalid_mask", "unsupported_image_model"}:
        return ImageFailure("invalid_request", explicit_rejection=True)
    if type(exc).__name__ == "ImagePollTimeoutError" or code == "upstream_image_timeout":
        return ImageFailure("result_recovery")
    if (isinstance(status, int) and status >= 500) or isinstance(exc, (ConnectionError, TimeoutError, OSError)) or any(word in text for word in ("curl:", "connection", "timeout", "overloaded", "network", "ssl", "tls", "certificate")):
        return ImageFailure("transient", True, min(900, 30 * 2 ** min(consecutive, 5)))
    # Text fallback is intentionally narrow; a generic 403 never disables an account.
    if any(word in text for word in ("content policy", "safety system", "moderation")):
        return ImageFailure("content_rejected", explicit_rejection=True)
    return ImageFailure("platform_error")
