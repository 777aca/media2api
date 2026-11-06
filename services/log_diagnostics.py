"""Evidence-only call diagnostics; no account configuration or credentials are read."""
from __future__ import annotations

import json


def task_type_for(endpoint: str) -> str:
    for fragment, kind in (("image", "image"), ("video", "video"), ("ppt", "ppt"),
                           ("psd", "psd"), ("search", "search"), ("chat", "chat"),
                           ("messages", "chat"), ("responses", "chat")):
        if fragment in endpoint:
            return kind
    return "other"


def call_diagnostics(call, result) -> dict:
    kind = "image" if call.image_task_id else task_type_for(call.endpoint)
    params = call.request_params or {}
    count = params.get("n")
    result = result if isinstance(result, dict) else {}
    # Protocol model fields often echo the request; they do not prove the backend model.
    return {
        "request_id": call.request_id, "task_id": call.image_task_id or None,
        "task_type": kind, "requested_model": call.model or None, "actual_model": None,
        "channel": None, "account_id": None, "account_email": None,
        "requested_count": count if type(count) is int and count > 0 else None,
        "output_count": len(result["data"]) if kind == "image" and isinstance(result.get("data"), list) else None,
        "queue_duration_ms": None, "execution_duration_ms": None,
        "first_token_ms": call.first_token_ms,
        "usage": result.get("usage") if isinstance(result.get("usage"), dict) else None,
        "quota_snapshot": None, "attempts": [],
    }


def has_text_delta(item) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("type") == "response.output_text.delta":
        return isinstance(item.get("delta"), str) and bool(item["delta"])
    delta = item.get("delta")
    if item.get("type") == "content_block_delta" and isinstance(delta, dict):
        return delta.get("type") == "text_delta" and bool(delta.get("text"))
    choices = item.get("choices")
    return isinstance(choices, list) and any(
        isinstance(choice, dict) and isinstance(choice.get("delta"), dict)
        and isinstance(choice["delta"].get("content"), str) and bool(choice["delta"]["content"])
        for choice in choices)


def matches_diagnostics(item, *, status="", channel="", model="", account="", task_type="", q=""):
    detail = item.get("detail")
    detail = detail if isinstance(detail, dict) else {}
    children = detail.get("image_tasks")
    records = [detail] + ([child for child in children if isinstance(child, dict)] if isinstance(children, list) else [])
    def values(*keys):
        return [str(record.get(key) or "").casefold() for record in records for key in keys]
    requested, output = detail.get("requested_count"), detail.get("output_count")
    parent_status = str(detail.get("status") or "").casefold()
    if parent_status == "partial" or detail.get("partial_success") is True or (
            type(requested) in (int, float) and type(output) in (int, float) and 0 < output < requested):
        parent_status = "partial"
    if parent_status == "error":
        parent_status = "failed"
    if status and ("failed" if status.casefold() == "error" else status.casefold()) != parent_status:
        return False
    aliases = {"web": "web", "chatgpt": "web", "chatgpt_web": "web", "grok": "grok_web"}
    canonical = lambda value: aliases.get(value.casefold(), value.casefold())
    if channel and canonical(channel) not in [canonical(value) for value in values("channel")]:
        return False
    if model and not any(model.casefold() in value for value in values("requested_model", "actual_model", "model")):
        return False
    if task_type and task_type.casefold() != str(detail.get("task_type") or task_type_for(str(detail.get("endpoint") or ""))).casefold():
        return False
    accounts = values("account_id", "account_email")
    for record in records:
        attempted = record.get("attempted_accounts")
        if isinstance(attempted, list):
            accounts.extend(value.casefold() for value in attempted if isinstance(value, str))
    if account and not any(account.casefold() in value for value in accounts):
        return False
    if q:
        # Search identifiers and safe summaries only, not arbitrary diagnostic payloads.
        searchable = [item.get("summary"), item.get("id")]
        for record in records:
            searchable.extend(record.get(key) for key in ("request_id", "task_id", "image_task_id", "image_child_id",
                              "conversation_id", "request_text", "request_params"))
        if q.casefold() not in json.dumps(searchable, ensure_ascii=False).casefold():
            return False
    return True

def normalize_call_detail(detail: dict) -> dict:
    """Give non-wrapper producers the same contract without inventing request IDs."""
    keys = ('request_id', 'task_id', 'actual_model', 'channel', 'account_id', 'account_email',
            'requested_count', 'output_count', 'queue_duration_ms', 'execution_duration_ms',
            'first_token_ms', 'usage', 'quota_snapshot')
    defaults = dict.fromkeys(keys)
    defaults.update({'task_type': 'image' if detail.get('image_task_id') else task_type_for(str(detail.get('endpoint') or '')),
                     'requested_model': detail.get('model') or None, 'attempts': [],
                     'task_id': detail.get('image_task_id') or None})
    return {**defaults, **detail}
