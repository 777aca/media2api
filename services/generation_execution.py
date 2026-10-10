"""Execute one image; once submitted, only result recovery is permitted."""
from __future__ import annotations

import base64
import json
import time

from services.generation_context import checkpoint, execution_checkpoint, ExecutionCheckpoint
from services.generation_errors import RetryImage, classify_image_error


def execute_image(request, index: int, total: int, row: dict):
    from services.protocol.conversation import account_service, OpenAIBackendAPI, ImageOutput, ImageGenerationError, format_image_result, stream_image_outputs, stream_codex_image_outputs, _web_image_25_error
    from utils.helper import is_codex_image_model
    from utils.log import logger
    account_service = row.get("_account_pool") or account_service

    current = execution_checkpoint.get()
    context_token = None
    if current is None:
        current = ExecutionCheckpoint(persist=lambda **kw: None, raw_writer=lambda items: None)
        context_token = execution_checkpoint.set(current)
    channel = "codex" if is_codex_image_model(request.model) else "web"
    attempted = set(json.loads(row.get("attempted_accounts") or "[]"))
    managed = bool(row.get("_account_token"))
    recovering = row.get("recovery") == "recovering_result"
    deadline = max(time.time() + 60, row.get("deadline", time.time() + 600)) if recovering else row.get("deadline", time.time() + 600)
    preferred = str(row.get("account_id") or "") if recovering else ""
    last_error = None
    try:
        if "raw_images" in row:
            data = format_image_result(row["raw_images"], request.prompt, request.response_format, request.base_url,
                                       requested_size=request.size, progress_callback=request.progress_callback)["data"]
            return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=row.get("conversation_id") or "")]
        while recovering or len(attempted) < 3:
            try:
                token = row["_account_token"] if managed else account_service.acquire_governed_image_token(model=request.model, channel=channel, excluded_ids=attempted, deadline=deadline, preferred_id=preferred)
            except Exception as exc:
                if last_error:
                    raise last_error from exc
                raise ImageGenerationError(str(exc), status_code=getattr(exc, "status_code", 502), code=getattr(exc, "code", "image_account_unavailable")) from exc
            account = account_service.get_account(token) or {}
            account_id = str(account.get("pool_account_id") or "")
            attempted.add(account_id)
            succeeded = False
            refreshed = False
            outputs = []
            try:
                if not recovering:
                    checkpoint("account_selected", account_id=account_id, attempt=len(attempted), conversation_id="")
                while True:
                    backend = OpenAIBackendAPI(access_token=token)
                    backend.progress_callback = request.progress_callback
                    try:
                        if recovering:
                            conversation_id = row.get("conversation_id") or ""
                            if not conversation_id or channel != "web":
                                raise ImageGenerationError("提交结果待确认，无法查询原请求", code="image_result_uncertain")
                            checkpoint("polling", conversation_id=conversation_id)
                            timeout = min(120, max(1, int((row.get("recovery_deadline") or time.time() + 120) - time.time())))
                            urls = backend.resolve_conversation_image_urls(conversation_id, [], [], poll_timeout_secs=timeout)
                            items = [{"b64_json": base64.b64encode(data).decode("ascii")} for data in backend.download_image_bytes(urls)]
                            if items:
                                from services.generation_context import save_raw_images
                                save_raw_images(items)
                            data = format_image_result(items, request.prompt, request.response_format, request.base_url,
                                                       requested_size=request.size, progress_callback=request.progress_callback)["data"]
                            if not data:
                                raise ImageGenerationError("原任务尚无可读取图片", code="upstream_image_timeout")
                            outputs = [ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)]
                        else:
                            stream_fn = stream_codex_image_outputs if channel == "codex" else stream_image_outputs
                            outputs = list(stream_fn(backend, request, index, total))
                        succeeded = any(output.kind == "result" and output.data for output in outputs)
                        if not succeeded:
                            raise ImageGenerationError("上游未生成图片", code="no_image_generated", status_code=400)
                        for output in outputs:
                            output.account_email = str(account.get("email") or "")
                        account_service.update_account(token, {"image_consecutive_failures": 0}, quiet=True)
                        return outputs
                    except Exception as exc:
                        failure = classify_image_error(exc, int(account.get("image_consecutive_failures") or 0))
                        phase = current.phase
                        if getattr(exc, "conversation_id", None):
                            checkpoint("polling", conversation_id=exc.conversation_id)
                            phase = current.phase
                        before_submission = phase in {"preparing", "account_selected", "rejected"} or (phase == "submitting" and not current.conversation_id and getattr(exc, "code", None) in {5, 6, 7, 35, 60})
                        # An HTTP rejection of a result poll says nothing about the earlier submission.
                        rejected_submission = failure.explicit_rejection and phase == "submitting" and not current.conversation_id
                        safe_retry = before_submission or rejected_submission
                        if safe_retry:
                            checkpoint("rejected", attempted_accounts=json.dumps(sorted(attempted)), error_category=failure.category, outcome="retry" if failure.retryable else "stop")
                        logger.warning({"event": "image_attempt_failed", "account_id": account_id, "category": failure.category,
                                        "phase": phase, "attempt": len(attempted), "outcome": "retry" if safe_retry and failure.retryable else "recover_or_stop"})
                        if safe_retry and failure.category == "credentials_invalid" and not refreshed:
                            refreshed = True
                            # This event deliberately suppresses the legacy password-login/removal side effect.
                            replacement = account_service.refresh_access_token(token, force=True, event="model_catalog_sync")
                            if replacement and replacement != token:
                                token = replacement
                                continue
                        if safe_retry or (phase == "submitting" and failure.category == "transient"):
                            account_service.record_image_failure(token, request.model, channel, failure)
                        # Keep sanitized structured fields, never upstream credential-bearing bodies.
                        message = {"rate_limited": "上游生图限流", "credentials_invalid": "生图账号凭据已失效", "model_permission": "账号没有该型号的生图权限",
                                   "access_denied": "上游暂时拒绝该生图通道", "transient": "上游网络或服务暂时不可用", "content_rejected": "上游拒绝了图片请求内容",
                                   "invalid_request": "上游未接受生图参数", "result_recovery": "生图结果读取超时，可继续恢复结果"}.get(failure.category, "生图处理失败，可查询任务状态")
                        error = ImageGenerationError(message, status_code=getattr(exc, "status_code", 502), code=failure.category,
                                                     conversation_id=current.conversation_id, account_email=str(account.get("email") or ""))
                        if channel == "web" and hasattr(exc, "body"):
                            error = _web_image_25_error(exc)
                        error.pool_account_id = account_id
                        error.failure_category = failure.category
                        # Preserve the certainty decision independently from the HTTP status of a later poll.
                        error.submission_uncertain = not safe_retry and failure.category not in {"content_rejected"}
                        if safe_retry:
                            checkpoint("rejected", error_category=failure.category, error_code=error.code, error=str(error), http_status=error.status_code)
                        if not recovering and safe_retry and failure.retryable and len(attempted) < 3:
                            if managed:
                                raise RetryImage() from error
                            last_error = error
                            break
                        raise error from exc
                    finally:
                        backend.close()
            finally:
                if managed:
                    account_service.mark_image_result(token, succeeded, release_slot=False)
                else:
                    account_service.mark_image_result(token, succeeded)
    finally:
        if context_token is not None:
            execution_checkpoint.reset(context_token)
