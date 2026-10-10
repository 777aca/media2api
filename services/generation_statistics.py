from __future__ import annotations

from collections import Counter
import math
import time


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    lower, upper = math.floor(position), math.ceil(position)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


def runtime_statistics(runtime, *, days: int = 1, model: str = "", channel: str = "", key_id: str = "", accounts: list | None = None) -> dict:
    store = runtime.store
    filters, parameters = ["j.legacy=0", "j.created>=?"], [time.time() - days * 86400]
    reject_filters, reject_parameters = ["created>=?"], [parameters[0]]
    for column, value in (("model", model), ("channel", channel), ("owner", key_id)):
        if value:
            filters.append(f"j.{column}=?")
            parameters.append(value)
            reject_filters.append(f"{column}=?")
            reject_parameters.append(value)
    rows = store.rows("SELECT t.*,j.model,j.channel,COALESCE(s.images,0) AS images FROM tasks t JOIN jobs j ON j.id=t.job_id LEFT JOIN settlements s ON s.task_id=t.id WHERE " + " AND ".join(filters), parameters)
    rejected = store.rows("SELECT * FROM rejections WHERE " + " AND ".join(reject_filters), reject_parameters)
    counts = Counter(row["status"] for row in rows)
    categories = Counter(row["error_category"] for row in rows if row["status"] == "error")
    for row in rejected:
        categories[row["category"]] += 1
    jobs: dict[str, list] = {}
    for row in rows:
        jobs.setdefault(row["job_id"], []).append(row)
    platform_failures = sum(1 for row in rows if row["status"] == "error" and row["error_category"] not in {"invalid_request", "content_rejected", "local_rejection"})
    samples = counts["success"] + platform_failures
    queues = [max(0, row["started"] - row["created"]) for row in rows if row["started"]]
    durations = [max(0, row["finished"] - row["started"]) for row in rows if row["started"] and row["finished"]]
    live = store.rows("SELECT owner,status,COUNT(*) AS count,SUM(CASE WHEN status='uncertain' OR recovery='recovering_result' THEN 1 ELSE 0 END) AS recovering FROM tasks WHERE status IN ('running','queued','uncertain') GROUP BY owner,status")
    per_key: dict[str, dict] = {}
    for row in live:
        item = per_key.setdefault(row["owner"], {"key_id": row["owner"], "running": 0, "queued": 0, "uncertain": 0, "recovering": 0})
        item[row["status"]] = row["count"]
        item["recovering"] += row["recovering"]
    settings = runtime.settings_getter()
    for key, value in per_key.items():
        value["concurrency_limit"] = store.quota(key)["image_concurrency_limit"] or settings["key_concurrency"]
    cooldowns = sum(1 for account in (accounts or []) if any(block.get("until") is not None and block["until"] > time.time() for block in (account.get("image_blocks") or {}).values()))
    return {"enabled_at": float(store.rows("SELECT value FROM metadata WHERE name='enabled_at'")[0]["value"]),
            "live": {"running": sum(row["count"] for row in live if row["status"] == "running"), "queued": sum(row["count"] for row in live if row["status"] == "queued"),
                     "uncertain": sum(row["count"] for row in live if row["status"] == "uncertain"), "recovering": sum(row["recovering"] for row in live), "cooldown_accounts": cooldowns, **settings, "keys": list(per_key.values())},
            "summary": {"requests": len(jobs) + len(rejected), "successful_images": sum(row["images"] for row in rows),
                        "successful_tasks": counts["success"], "partial_success": sum(1 for values in jobs.values() if any(row["status"] == "success" for row in values) and any(row["status"] != "success" for row in values) and all(row["status"] not in {"queued", "running", "uncertain"} for row in values)),
                        "recovering": sum(1 for row in rows if row["status"] == "uncertain" or (row["status"] in {"queued", "running"} and row["recovery"] == "recovering_result")),
                        "failed": counts["error"], "cancelled": counts["cancelled"], "uncertain": counts["uncertain"], "rejected": len(rejected),
                        "platform_failures": platform_failures, "invalid_request": categories["invalid_request"], "content_rejected": categories["content_rejected"], "local_rejection": categories["local_rejection"],
                        "platform_success_rate": round(counts["success"] / samples, 4) if samples else None,
                        "queue_average": round(sum(queues) / len(queues), 3) if queues else None,
                        "generation_average": round(sum(durations) / len(durations), 3) if durations else None,
                        "queue_p50": percentile(queues, .5), "queue_p95": percentile(queues, .95), "generation_p50": percentile(durations, .5), "generation_p95": percentile(durations, .95)},
            "filters": {"models": [r["model"] for r in store.rows("SELECT model FROM jobs WHERE legacy=0 UNION SELECT model FROM rejections ORDER BY model")],
                        "keys": [r["owner"] for r in store.rows("SELECT owner FROM jobs WHERE legacy=0 UNION SELECT owner FROM rejections ORDER BY owner")]}}
