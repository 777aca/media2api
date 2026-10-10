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
            merged.setdefault(field, value)
    return merged


def runtime_call_details(job_id: str, owner: str) -> dict:
    from services.generation_runtime import get_generation_runtime

    rows = get_generation_runtime().store.rows(
        "SELECT t.id,t.error_category,t.phase,t.attempt,t.recovery,t.status,t.error_code,"
        "t.recovery_attempts,t.recovery_deadline,j.channel,COALESCE((SELECT e.phase FROM events e "
        "WHERE e.task_id=t.id AND e.outcome=t.status ORDER BY e.id DESC LIMIT 1),t.phase) AS execution_phase "
        "FROM tasks t JOIN jobs j ON j.id=t.job_id WHERE j.id=? AND j.owner=? ORDER BY t.ordinal",
        (job_id, owner),
    )
    return with_task_diagnostics({}, [task_diagnostics(row) for row in rows])


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
