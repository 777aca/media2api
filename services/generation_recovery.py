"""Bounded, persistent recovery of existing image results; never submit images."""
from __future__ import annotations

from pathlib import Path
import time

RECOVERY_DELAYS = (30, 60, 120)
RECOVERY_TIMEOUT_SECONDS = 600


class ResultRecovery:
    def __init__(self, store, directory: Path):
        self.store = store
        self.directory = directory

    def result_phase(self, row: dict) -> str | None:
        directory = self.directory / "generation-tasks" / row["job_id"]
        if (directory / f"{row['id']}-output.json").is_file():
            return "output_saved"
        if (directory / f"{row['id']}-raw.json").is_file():
            return "raw_saved"
        if row["channel"] == "web" and row["conversation_id"] and row["account_id"]:
            return "polling"
        return None

    def finish_failed(self, task_id: str, code: str, reason: str) -> None:
        self.store.finish(task_id, status="error", category="result_recovery", code=code,
                          error=f"{reason}；任务已自动结束并释放本地预占额度，未重新生图",
                          http_status=504 if code != "image_result_unrecoverable" else 502,
                          recovery_status="auto_failed")

    def advance(self, active: set[str]) -> list[tuple[str, str]]:
        now = time.time()
        rows = self.store.rows("SELECT t.*,j.channel FROM tasks t JOIN jobs j ON j.id=t.job_id "
                               "WHERE t.status='uncertain' OR (t.status='queued' AND t.recovery='recovering_result')")
        finished = []
        for row in rows:
            if row["id"] in active:
                continue
            phase = self.result_phase(row)
            code = reason = ""
            if phase is None:
                code, reason = "image_result_unrecoverable", "缺少可读取的原会话或已保存结果，无法确认上游结果"
            elif row["recovery_deadline"] is not None and row["recovery_deadline"] <= now:
                code, reason = "image_result_recovery_expired", "自动读取原结果已超过 10 分钟"
            elif row["recovery_attempts"] >= len(RECOVERY_DELAYS):
                code, reason = "image_result_recovery_exhausted", "自动读取原结果 3 次后仍未成功"
            if code:
                self.finish_failed(row["id"], code, reason)
                finished.append((row["id"], row["phase"]))
                continue
            # A saved result, or a known submitted request recovered on startup,
            # may be read immediately. Subsequent failures use bounded backoff.
            delay = 0 if row["status"] == "queued" or (phase in {"raw_saved", "output_saved"} and row["recovery_attempts"] == 0) else RECOVERY_DELAYS[row["recovery_attempts"]]
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET phase=?,recovery=CASE WHEN status='uncertain' THEN 'auto_pending' ELSE recovery END,recovery_started=COALESCE(recovery_started,?),"
                           "recovery_deadline=COALESCE(recovery_deadline,?),recovery_next_at=COALESCE(recovery_next_at,?) "
                           "WHERE id=? AND status IN ('uncertain','queued')",
                           (phase, now, now + RECOVERY_TIMEOUT_SECONDS, now + delay, row["id"]))
                db.execute("UPDATE tasks SET status='queued',recovery='recovering_result',updated=? "
                           "WHERE id=? AND status='uncertain' AND recovery_next_at<=?",
                           (now, row["id"], now))
        return finished


def recovery_details(row: dict) -> dict:
    return {"recovery_attempts": row["recovery_attempts"], "recovery_max_attempts": len(RECOVERY_DELAYS),
            "recovery_next_at": row["recovery_next_at"], "recovery_deadline": row["recovery_deadline"]}
