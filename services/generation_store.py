"""Durable image tasks, quota reservations and idempotent settlements (single instance)."""
from __future__ import annotations

from contextlib import contextmanager, closing
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from services.generation_errors import GenerationRuntimeError

TERMINAL = {"success", "error", "cancelled"}
ACTIVE = {"queued", "running", "uncertain"}
_TASK_METADATA = ",".join(f"t.{column}" for column in (
    "id", "job_id", "ordinal", "owner", "status", "phase", "created", "started", "finished", "updated",
    "account_id", "conversation_id", "attempt", "recovery", "error_category", "error_code", "error",
    "http_status", "reservation", "attempted_accounts", "recovery_attempts", "recovery_started",
    "recovery_deadline", "recovery_next_at",
))


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def image_metadata(outputs: list) -> list[dict]:
    return [{key: value for key, value in image.items() if key != "b64_json"}
            for output in outputs for image in output.get("data", [])]


class GenerationStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS key_usage (
                owner TEXT PRIMARY KEY, quota_limit INTEGER, concurrency_limit INTEGER,
                used INTEGER NOT NULL DEFAULT 0 CHECK(used>=0), reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, identity TEXT NOT NULL, endpoint TEXT NOT NULL,
                external_id TEXT, fingerprint TEXT NOT NULL, payload TEXT NOT NULL, model TEXT NOT NULL,
                channel TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL, legacy INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs(owner,endpoint,external_id,created);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), ordinal INTEGER NOT NULL,
                owner TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL, created REAL NOT NULL,
                started REAL, finished REAL, updated REAL NOT NULL, account_id TEXT, conversation_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 0, recovery TEXT NOT NULL DEFAULT '', error_category TEXT,
                error_code TEXT, error TEXT, http_status INTEGER, output TEXT, reservation INTEGER NOT NULL DEFAULT 1,
                UNIQUE(job_id,ordinal)
            );
            CREATE INDEX IF NOT EXISTS task_dispatch ON tasks(status,owner,created,ordinal);
            CREATE TABLE IF NOT EXISTS task_output_metadata (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id), data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settlements (
                task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, images INTEGER NOT NULL, released INTEGER NOT NULL,
                outcome TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, phase TEXT NOT NULL, category TEXT,
                attempt INTEGER, outcome TEXT, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY, owner TEXT, model TEXT, channel TEXT, category TEXT, code TEXT, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "attempted_accounts" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN attempted_accounts TEXT NOT NULL DEFAULT '[]'")
        for name, definition in {"recovery_attempts": "INTEGER NOT NULL DEFAULT 0", "recovery_started": "REAL",
                                 "recovery_deadline": "REAL", "recovery_next_at": "REAL"}.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('enabled_at',?)", (str(time.time()),))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def rows(self, query: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute(query, params).fetchall()]

    def quota(self, owner: str) -> dict:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO key_usage(owner) VALUES (?)", (owner,))
            row = dict(self.db.execute("SELECT * FROM key_usage WHERE owner=?", (owner,)).fetchone())
        return {"image_quota_limit": row["quota_limit"], "image_concurrency_limit": row["concurrency_limit"],
                "image_quota_used": row["used"], "image_quota_reserved": row["reserved"],
                "image_quota_remaining": None if row["quota_limit"] is None else row["quota_limit"] - row["used"] - row["reserved"]}

    def set_limits(self, owner: str, updates: dict) -> dict:
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO key_usage(owner) VALUES (?)", (owner,))
            row = db.execute("SELECT * FROM key_usage WHERE owner=?", (owner,)).fetchone()
            for field, column in (("image_quota_limit", "quota_limit"), ("image_concurrency_limit", "concurrency_limit")):
                if field not in updates:
                    continue
                value = updates[field]
                if value is not None and (type(value) is not int or value < (1 if column == "concurrency_limit" else 0)):
                    raise ValueError("图片额度必须是非负整数，并发必须是正整数；留空使用默认值")
                if column == "quota_limit" and value is not None and value < row["used"] + row["reserved"]:
                    raise ValueError("额度上限不能低于已用张数与预占张数之和")
                db.execute(f"UPDATE key_usage SET {column}=? WHERE owner=?", (value, owner))
        return self.quota(owner)

    def find_duplicate(self, owner: str, endpoint: str, external_id: str, fingerprint: str) -> str | None:
        if not external_id:
            return None
        rows = self.rows("SELECT id,fingerprint FROM jobs WHERE owner=? AND endpoint=? AND external_id=? AND created>? ORDER BY created DESC LIMIT 1",
                         (owner, endpoint, external_id, time.time() - 30 * 86400))
        if not rows:
            return None
        if rows[0]["fingerprint"] != fingerprint:
            raise GenerationRuntimeError("幂等标识已用于不同的请求内容", "idempotency_conflict", 409)
        return rows[0]["id"]

    def submit(self, *, job_id: str, identity: dict, endpoint: str, external_id: str, fingerprint: str,
               payload: dict, channel: str, count: int, settings: dict) -> str:
        now = time.time()
        owner = str(identity["id"])
        with self.transaction() as db:
            existing = self.find_duplicate(owner, endpoint, external_id, fingerprint)
            if existing:
                return existing
            db.execute("INSERT OR IGNORE INTO key_usage(owner) VALUES (?)", (owner,))
            usage = db.execute("SELECT * FROM key_usage WHERE owner=?", (owner,)).fetchone()
            if identity.get("role") != "admin" and usage["quota_limit"] is not None and usage["used"] + usage["reserved"] + count > usage["quota_limit"]:
                raise GenerationRuntimeError("API Key 剩余图片额度不足", "image_quota_exceeded")
            queued = db.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
            if queued + count > settings["max_waiting_images"]:
                raise GenerationRuntimeError("生图等待队列已满，请稍后重试", "image_queue_full")
            db.execute("INSERT INTO jobs(id,owner,identity,endpoint,external_id,fingerprint,payload,model,channel,created,deadline) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, owner, dump(identity), endpoint, external_id or None, fingerprint, dump(payload), payload["model"], channel, now, now + settings["queue_timeout_seconds"]))
            for ordinal in range(count):
                db.execute("INSERT INTO tasks(id,job_id,ordinal,owner,status,phase,created,updated) VALUES (?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, job_id, ordinal + 1, owner, "queued", "queued", now, now))
            db.execute("UPDATE key_usage SET reserved=reserved+? WHERE owner=?", (count, owner))
        return job_id

    def reject(self, identity: dict, model: str, channel: str, code: str, category: str = "local_rejection") -> None:
        with self.lock:
            self.db.execute("INSERT INTO rejections(owner,model,channel,category,code,created) VALUES (?,?,?,?,?,?)",
                            (identity.get("id"), model, channel, category, code, time.time()))

    def task(self, task_id: str, *, include_output: bool = True) -> dict:
        columns = _TASK_METADATA + (",t.output" if include_output else "")
        rows = self.rows(f"SELECT {columns},j.payload,j.identity,j.endpoint,j.model,j.channel,j.deadline FROM tasks t JOIN jobs j ON j.id=t.job_id WHERE t.id=?", (task_id,))
        if not rows:
            raise KeyError(task_id)
        return rows[0]

    def save_output_metadata(self, task_id: str, outputs: list) -> None:
        with self.lock:
            self.db.execute("INSERT INTO task_output_metadata(task_id,data) VALUES (?,?) "
                            "ON CONFLICT(task_id) DO UPDATE SET data=excluded.data",
                            (task_id, dump(image_metadata(outputs))))

    def public_tasks(self, job_id: str) -> list[dict]:
        with self.lock:
            # Upgrade old results lazily, one body at a time. A separate table avoids
            # rewriting all Base64 overflow pages just to add a small preview.
            missing = self.db.execute(
                "SELECT t.id FROM tasks t LEFT JOIN task_output_metadata m ON m.task_id=t.id "
                "WHERE t.job_id=? AND t.status IN ('success','error','cancelled') AND m.task_id IS NULL", (job_id,),
            ).fetchall()
            for row in missing:
                body = self.db.execute("SELECT output FROM tasks WHERE id=?", (row["id"],)).fetchone()[0]
                self.save_output_metadata(row["id"], json.loads(body or "[]"))
                del body
            return self.rows(f"SELECT {_TASK_METADATA},m.data AS image_data FROM tasks t "
                             "LEFT JOIN task_output_metadata m ON m.task_id=t.id WHERE t.job_id=? ORDER BY t.ordinal", (job_id,))

    def output(self, task_id: str) -> list | None:
        with self.lock:
            row = self.db.execute("SELECT output FROM tasks WHERE id=?", (task_id,)).fetchone()
            return json.loads(row[0]) if row and row[0] is not None else None

    def expire_outputs(self, job_id: str, cutoff: float) -> bool:
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM tasks WHERE job_id=? AND (status IN ('queued','running','uncertain') "
                          "OR COALESCE(finished,updated)>=?) LIMIT 1", (job_id, cutoff)).fetchone():
                return False
            self.public_tasks(job_id)
            db.execute("UPDATE tasks SET output=NULL WHERE job_id=? AND output IS NOT NULL", (job_id,))
        return True

    def checkpoint(self, task_id: str, phase: str | None = None, **values) -> None:
        allowed = {"account_id", "conversation_id", "attempt", "attempted_accounts", "recovery", "error_category", "error_code", "error", "http_status"}
        changes = {key: value for key, value in values.items() if key in allowed}
        if phase:
            changes["phase"] = phase
        changes["updated"] = time.time()
        with self.transaction() as db:
            row = db.execute("SELECT status,phase,attempt FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] in TERMINAL:
                raise GenerationRuntimeError("任务已经结束", "image_task_ended", 409)
            db.execute("UPDATE tasks SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?", (*changes.values(), task_id))
            db.execute("INSERT INTO events(task_id,phase,category,attempt,outcome,created) VALUES (?,?,?,?,?,?)",
                       (task_id, phase or row["phase"], values.get("error_category"), values.get("attempt", row["attempt"]), values.get("outcome"), time.time()))

    def finish(self, task_id: str, *, status: str, output: list | None = None, category: str = "", code: str = "", error: str = "", http_status: int = 502, recovery_status: str | None = None) -> list:
        """The terminal state, reservation release and charge are one transaction."""
        now = time.time()
        with self.transaction() as db:
            task = db.execute("SELECT t.*,j.identity FROM tasks t JOIN jobs j ON j.id=t.job_id WHERE t.id=?", (task_id,)).fetchone()
            if task["status"] in TERMINAL:
                return json.loads(task["output"] or "[]")
            if status == "uncertain":
                db.execute("UPDATE tasks SET status='uncertain',recovery='auto_pending',recovery_next_at=NULL,error_category=?,error_code=?,error=?,http_status=?,updated=? WHERE id=?",
                           (category, code or "image_result_uncertain", error, http_status, now, task_id))
                return []
            outputs = output or []
            count = sum(len(item.get("data", [])) for item in outputs if item.get("kind") == "result")
            reserved = task["reservation"]
            usage = db.execute("SELECT * FROM key_usage WHERE owner=?", (task["owner"],)).fetchone()
            # Extra upstream pictures must fit within currently unreserved quota.
            if count > reserved and json.loads(task["identity"]).get("role") != "admin" and usage["quota_limit"] is not None:
                permitted = reserved + max(0, usage["quota_limit"] - usage["used"] - usage["reserved"])
                count = min(count, permitted)
                left = count
                trimmed = []
                for item in outputs:
                    if item.get("kind") == "result":
                        item = {**item, "data": item.get("data", [])[:left]}
                        left -= len(item["data"])
                        if not item["data"]:
                            continue
                    trimmed.append(item)
                outputs = trimmed
            db.execute("INSERT INTO settlements(task_id,owner,images,released,outcome,created) VALUES (?,?,?,?,?,?)",
                       (task_id, task["owner"], count, reserved, status, now))
            db.execute("UPDATE key_usage SET used=used+?,reserved=reserved-? WHERE owner=?", (count, reserved, task["owner"]))
            db.execute("UPDATE tasks SET status=?,phase=?,finished=?,updated=?,output=?,reservation=0,error_category=?,error_code=?,error=?,http_status=?,recovery_next_at=NULL,recovery=COALESCE(?,CASE WHEN recovery='' THEN '' ELSE 'completed' END) WHERE id=?",
                       (status, status, now, now, dump(outputs), category, code, error, http_status, recovery_status, task_id))
            self.save_output_metadata(task_id, outputs)
            db.execute("INSERT INTO events(task_id,phase,category,attempt,outcome,created) VALUES (?,?,?,?,?,?)",
                       (task_id, task["phase"], category, task["attempt"], status, now))
        return outputs

    def claim(self, task_id: str, *, account_id: str | None = None, attempt: int | None = None) -> bool:
        with self.lock:
            cursor = self.db.execute("UPDATE tasks SET status='running',phase=CASE WHEN recovery='recovering_result' THEN phase WHEN ? IS NOT NULL THEN 'account_selected' ELSE 'preparing' END,account_id=COALESCE(?,account_id),attempt=COALESCE(?,attempt),recovery_attempts=recovery_attempts+CASE WHEN recovery='recovering_result' THEN 1 ELSE 0 END,recovery_next_at=NULL,started=COALESCE(started,?),updated=? WHERE id=? AND status='queued'", (account_id, account_id, attempt, time.time(), time.time(), task_id))
            return cursor.rowcount == 1

    def requeue_attempt(self, task_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE tasks SET status='queued',recovery='retrying_account',updated=? WHERE id=? AND status='running' AND phase='rejected'", (time.time(), task_id))

    def recover(self) -> None:
        # No submitted child is ever reset to a new generation.
        for row in self.rows("SELECT * FROM tasks WHERE status='running'"):
            phase = row["phase"]
            if phase in {"queued", "preparing", "account_selected", "rejected"}:
                status, recovery = "queued", "requeued"
            elif phase in {"raw_saved", "output_saved"} or row["conversation_id"]:
                status, recovery = "queued", "recovering_result"
            else:
                status, recovery = "uncertain", "auto_pending"
            with self.lock:
                self.db.execute("UPDATE tasks SET status=?,recovery=?,updated=?,error=CASE WHEN ?='uncertain' THEN '上游结果未取回，系统将自动处理；不会重新生图' ELSE error END WHERE id=?", (status, recovery, time.time(), status, row["id"]))

    def snapshot(self, target: Path) -> None:
        with self.lock, closing(sqlite3.connect(target)) as destination:
            self.db.backup(destination)

    def close(self) -> None:
        with self.lock:
            self.db.close()
