"""Keep image execution diagnostics inside their corresponding call log."""
from __future__ import annotations


DIAGNOSTIC_FIELDS = (
    "image_child_id", "channel", "error_category", "phase", "retry_count",
    "recovery_status", "outcome", "error_code",
    "recovery_attempts", "recovery_deadline",
)


def task_diagnostics(row: dict, phase: str | None = None) -> dict:
    return {
        "image_child_id": row["id"], "channel": row["channel"],
        "error_category": row["error_category"],
        "phase": phase or row.get("execution_phase") or row["phase"],
        "retry_count": max(0, row["attempt"] - 1),
        "recovery_status": row["recovery"], "outcome": row["status"],
        "error_code": row["error_code"],
        "recovery_attempts": row["recovery_attempts"], "recovery_deadline": row["recovery_deadline"],
    }


def with_task_diagnostics(detail: dict, tasks: list[dict]) -> dict:
    if not tasks:
        return detail
    merged = dict(detail)
    merged["image_tasks"] = tasks
    if len(tasks) == 1:
        for field, value in tasks[0].items():
            if merged.get(field) is None:
                merged[field] = value
    else:
        model = tasks[0].get("actual_model")
        same_model = isinstance(model, str) and bool(model) and all(task.get("actual_model") == model for task in tasks)
        merged["actual_model"] = model if same_model else None
        source = tasks[0].get("actual_model_source")
        merged["actual_model_source"] = source if same_model and source in {"upstream_request", "upstream_response"} and all(
            task.get("actual_model_source") == source for task in tasks) else None
    return merged


def runtime_call_details(job_id: str, owner: str, *, store=None) -> dict:
    import json
    from services.generation_runtime import get_generation_runtime

    store = store if store is not None else get_generation_runtime().store
    # Select only metadata, never job identity/payload, raw outputs or account secrets.
    rows = store.rows(
        "SELECT t.id,t.error_category,t.phase,t.attempt,t.recovery,t.status,t.error_code,"
        "t.created,t.started,t.finished,t.account_id,t.conversation_id,t.attempted_accounts,t.diagnostics,"
        "s.images AS output_count,t.recovery_attempts,t.recovery_deadline,j.channel,"
        "COALESCE((SELECT e.phase FROM events e WHERE e.task_id=t.id AND e.outcome=t.status "
        "ORDER BY e.id DESC LIMIT 1),t.phase) AS execution_phase "
        "FROM tasks t JOIN jobs j ON j.id=t.job_id LEFT JOIN settlements s ON s.task_id=t.id "
        "WHERE j.id=? AND j.owner=? ORDER BY t.ordinal", (job_id, owner),
    )
    if not rows:
        return {}
    tasks = []
    for row in rows:
        child = task_diagnostics(row)
        events = store.rows("SELECT phase,category,attempt,outcome,created,account_id,account_email,error_code,diagnostics FROM events WHERE task_id=? ORDER BY id", (row["id"],))
        # New events capture identity at write time. Old events stay unknown.
        for event in events:
            event["diagnostics"] = json.loads(event["diagnostics"] or "{}")
        attempts = {}
        for event in events:
            number = event["attempt"]
            if not number or number < 1:
                continue
            attempt = attempts.setdefault(number, {"attempt": number, "account_id": None,
                "account_email": None, "phase": None, "status": None, "error_code": None,
                "error_category": None, "duration_ms": None})
            for field in ("account_id", "account_email", "error_code"):
                if event.get(field) is not None:
                    attempt[field] = event[field]
            attempt.update(event["diagnostics"])
            attempt["phase"] = event["phase"]
            if event["outcome"] is not None:
                attempt["status"] = event["outcome"]
            if event["category"] is not None:
                attempt["error_category"] = event["category"]
        child.update({"task_id": row["id"], "account_id": row["account_id"],
                      "account_email": None, "conversation_id": row["conversation_id"],
                      "attempted_accounts": json.loads(row["attempted_accounts"] or "[]"),
                      "events": events, "attempts": list(attempts.values()),
                      "output_count": row["output_count"],
                      "queue_duration_ms": _duration_ms(row["created"], row["started"]),
                      "execution_duration_ms": _duration_ms(row["started"], row["finished"])})
        snapshot = json.loads(row["diagnostics"] or "{}")
        # A task's current identity is authoritative; never attach another account's
        # snapshot to it (including pre-isolation persisted records).
        if not snapshot.get("account_id") or snapshot["account_id"] == row["account_id"]:
            child.update({key: value for key, value in snapshot.items() if not key.startswith("_")})
        tasks.append(child)
    detail = with_task_diagnostics({}, tasks)
    detail.update({"task_id": job_id, "task_type": "image", "channel": rows[0]["channel"],
                   "requested_count": len(rows), "attempts": tasks[0]["attempts"] if len(tasks) == 1 else [],
                   "output_count": sum(row["output_count"] for row in rows) if all(row["output_count"] is not None for row in rows) else None})
    if detail["output_count"] is not None:
        detail["partial_success"] = 0 < detail["output_count"] < len(rows)
    # Job wall-clock spans, not sums of parallel child durations.
    starts = [row["started"] for row in rows if row["started"] is not None]
    detail["queue_duration_ms"] = _duration_ms(min(row["created"] for row in rows), min(starts)) if starts else None
    detail["execution_duration_ms"] = (_duration_ms(min(starts), max(row["finished"] for row in rows))
        if starts and all(row["finished"] is not None for row in rows) else None)
    detail["attempted_accounts"] = sorted({value for task in tasks
        for value in [*task.get("attempted_accounts", []), *(event.get("account_id") for event in task["events"])] if value})
    detail["attempted_accounts_order"] = "unordered"
    return detail


def _duration_ms(start, end):
    return max(0, int(round((end - start) * 1000))) if start is not None and end is not None else None


def legacy_call_key(item: dict) -> tuple[str, str, str] | None:
    detail = item.get("detail")
    if item.get("type") != "call" or not isinstance(detail, dict):
        return None
    owner, endpoint, job = (detail.get(field) for field in ("key_id", "endpoint", "image_task_id"))
    if all(isinstance(value, str) and value for value in (owner, endpoint, job)) and endpoint.startswith("/v1/"):
        return owner, endpoint, job
    return None


def is_legacy_task_log(item: dict) -> bool:
    return item.get("summary") == "生图任务状态" and legacy_call_key(item) is not None


def legacy_task_companions(items: list[dict]) -> dict[str, list[dict]]:
    """Match exact owner/endpoint/job IDs; never infer identity from time or prompt."""
    diagnostics: dict[tuple[str, str, str], dict[str, dict]] = {}
    for item in items:  # newest first: preserve the latest diagnostic per child
        if is_legacy_task_log(item):
            child_id = item["detail"].get("image_child_id")
            if isinstance(child_id, str) and child_id:
                diagnostics.setdefault(legacy_call_key(item), {}).setdefault(child_id, item)
    return {
        item["id"]: list(diagnostics[key].values())
        for item in items
        if not is_legacy_task_log(item) and (key := legacy_call_key(item)) in diagnostics
    }


def collapse_legacy_task_logs(items: list[dict]) -> list[dict]:
    companions = legacy_task_companions(items)
    matched_keys = {legacy_call_key(item) for item in items if item["id"] in companions}
    result = []
    for item in items:
        if is_legacy_task_log(item) and legacy_call_key(item) in matched_keys:
            continue
        if item["id"] in companions and not item["detail"].get("image_tasks"):
            tasks = [{field: child["detail"].get(field) for field in DIAGNOSTIC_FIELDS}
                     for child in companions[item["id"]]]
            item = {**item, "detail": with_task_diagnostics(item["detail"], tasks)}
        result.append(item)
    return result
