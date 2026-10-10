from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from services.generation_context import ExecutionCheckpoint, GenerationContext, execution_checkpoint
from services.generation_errors import GenerationRuntimeError, RetryImage, classify_image_error
from services.generation_store import GenerationStore, TERMINAL, dump
from services.generation_recovery import ResultRecovery, recovery_details

DEFAULT_QUEUE = {"global_concurrency": 8, "key_concurrency": 4, "max_waiting_images": 200,
                 "queue_timeout_seconds": 600, "task_retention_days": 30}


def queue_settings(value: object) -> dict:
    value = value if isinstance(value, dict) else {}
    settings = dict(DEFAULT_QUEUE)
    for key, default in settings.items():
        candidate = value.get(key, default)
        maximum = None if "concurrency" in key else (10000 if key == "max_waiting_images" else 2592000)
        if type(candidate) is not int or candidate < 1 or (maximum is not None and candidate > maximum):
            constraint = "正整数" if maximum is None else f"1 至 {maximum} 的整数"
            raise ValueError(f"{key} 必须是{constraint}")
        settings[key] = candidate
    settings["task_retention_days"] = max(30, settings["task_retention_days"])
    return settings


def _iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


class GenerationRuntime:
    def __init__(self, directory: Path, *, settings_getter=None, identity_resolver=None, executor=None, account_pool=None):
        self.directory = directory
        self.store = GenerationStore(directory / "generation-runtime.sqlite")
        self.recovery = ResultRecovery(self.store, directory)
        self.settings_getter = settings_getter or (lambda: DEFAULT_QUEUE)
        self.identity_resolver = identity_resolver or self._resolve_identity
        self.execute = executor or self._execute_image
        self.account_pool = account_pool
        self._requires_account = executor is None or account_pool is not None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._active: dict[str, str] = {}
        self._owners: deque[str] = deque()
        self._last_owner = ""
        self._pool = None
        self._pool_workers = 0
        self._retired_pools: list[ThreadPoolExecutor] = []
        self._thread = None
        self._lease = None
        self._last_cleanup = 0.0

    @staticmethod
    def _resolve_identity(owner: str) -> dict | None:
        if owner == "admin":
            return {"id": "admin", "role": "admin", "enabled": True, "name": "管理员"}
        from services.auth_service import auth_service
        return auth_service.find_identity(owner)

    @staticmethod
    def _execute_image(request, index: int, total: int, row: dict):
        from services.generation_execution import execute_image
        return execute_image(request, index, total, row)

    def start(self) -> None:
        with self._lock:
            if self._thread:
                return
            lease = open(self.directory / "generation-runtime.lock", "a+b")
            try:
                lease.seek(0)
                if os.name == "nt":
                    import msvcrt
                    if not lease.read(1):
                        lease.write(b"0")
                        lease.flush()
                    lease.seek(0)
                    msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                lease.close()
                raise RuntimeError("同一数据目录只能启动一个生图调度器；请使用单 API worker") from exc
            self._lease = lease
            self._stop.clear()
            self.store.recover()
            self._import_legacy()
            self._resize_pool(self.settings_getter()["global_concurrency"])
            self._thread = threading.Thread(target=self._dispatch, name="image-dispatcher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._pool:
            # Hold the directory lease until all upstream activity has stopped.
            self._pool.shutdown(wait=True, cancel_futures=False)
        for pool in self._retired_pools:
            pool.shutdown(wait=True, cancel_futures=False)
        self._pool = None
        self._pool_workers = 0
        self._retired_pools.clear()
        self._thread = None
        if self._lease:
            self._lease.close()
            self._lease = None

    def _resize_pool(self, max_workers: int) -> None:
        if self._pool_workers == max_workers:
            return
        replacement = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="image-worker")
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=False)
            self._retired_pools.append(self._pool)
        self._pool = replacement
        self._pool_workers = max_workers

    def _private_path(self, job_id: str, filename: str) -> Path:
        if not job_id.isalnum() or Path(filename).name != filename:
            raise ValueError("invalid task path")
        directory = self.directory / "generation-tasks" / job_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory / filename

    def _write_json(self, job_id: str, filename: str, value) -> None:
        target = self._private_path(job_id, filename)
        temporary = target.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(dump(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def submit(self, request, context: GenerationContext) -> str:
        from utils.helper import is_codex_image_model
        self.start()
        owner = str(context.identity.get("id") or "")
        identity = self.identity_resolver(owner)
        if not identity or not identity.get("enabled", True):
            raise GenerationRuntimeError("API Key 已禁用或删除", "image_key_disabled", 403)
        payload = {field.name: getattr(request, field.name) for field in fields(request) if field.name not in {"progress_callback", "generation_context"}}
        if type(request.n) is not int or not 1 <= request.n <= 16:
            raise GenerationRuntimeError("n 必须是 1 至 16 的整数", "invalid_image_count", 400)
        # URLs used only for output rendering must not break idempotency on reconnect.
        fingerprint = hashlib.sha256(dump({k: v for k, v in payload.items() if k != "base_url"}).encode()).hexdigest()
        channel = "codex" if is_codex_image_model(request.model) else "web"
        if context.client_task_ids:
            if len(context.client_task_ids) != request.n or len(set(context.client_task_ids)) != request.n:
                raise GenerationRuntimeError("图片任务标识必须唯一且与张数一致", "invalid_image_request", 400)
            payload["client_task_ids"] = context.client_task_ids
        fingerprint = hashlib.sha256(dump({k: v for k, v in payload.items() if k != "base_url"}).encode()).hexdigest()
        if len(context.idempotency_key) > 256:
            raise GenerationRuntimeError("Idempotency-Key 长度不能超过 256", "invalid_idempotency_key", 400)
        with self._lock:
            job_id = ""
            try:
                existing = self.store.find_duplicate(owner, context.endpoint, context.idempotency_key, fingerprint)
                if existing:
                    context.task_id = existing
                    return existing
                job_id = uuid.uuid4().hex
                images, mask = payload.pop("images", None), payload.pop("mask", None)
                if images or mask:
                    self._write_json(job_id, "inputs.json", {"images": images, "mask": mask})
                    payload["has_inputs"] = True
                task_id = self.store.submit(job_id=job_id, identity=identity, endpoint=context.endpoint,
                                            external_id=context.idempotency_key, fingerprint=fingerprint,
                                            payload=payload, channel=channel, count=request.n, settings=self.settings_getter())
            except GenerationRuntimeError as exc:
                if job_id:
                    orphan = self.directory / "generation-tasks" / job_id
                    (orphan / "inputs.json").unlink(missing_ok=True)
                    if orphan.exists():
                        orphan.rmdir()
                self.store.reject(identity, request.model, channel, exc.code)
                exc.image_rejection_recorded = True
                raise
        context.task_id = task_id
        self._wake.set()
        return task_id

    def _dispatch(self) -> None:
        from utils.log import logger
        while not self._stop.is_set():
            try:
                self.dispatch_once()
                if time.time() - self._last_cleanup > 3600:
                    self.cleanup()
                    self._last_cleanup = time.time()
            except Exception as exc:
                logger.error({"event": "image_dispatch_error", "error_type": type(exc).__name__})
            self._wake.wait(0.25)
            self._wake.clear()

    def dispatch_once(self) -> None:
        settings = self.settings_getter()
        with self._lock:
            for task_id, phase in self.recovery.advance(set(self._active)):
                self._log_task(task_id, phase)
        waiting = self.store.rows("SELECT t.*,j.deadline,j.model,j.channel FROM tasks t JOIN jobs j ON j.id=t.job_id WHERE t.status='queued' ORDER BY t.created,t.ordinal")
        by_owner: dict[str, list] = {}
        identities = {}
        for row in waiting:
            if row["id"] in self._active:
                continue
            owner = row["owner"]
            if owner not in identities:
                identities[owner] = self.identity_resolver(owner)
            identity = identities[owner]
            submitted = row["recovery"] == "recovering_result"
            if not submitted and row["reservation"] < 1:
                self.store.finish(row["id"], status="error", category="local_rejection", code="image_quota_reservation_missing", error="任务额度预占不存在", http_status=409)
            elif not submitted and (not identity or not identity.get("enabled", True)):
                self.store.finish(row["id"], status="cancelled", category="local_rejection", code="image_key_disabled", error="API Key 已禁用或删除", http_status=403)
            elif not submitted and row["deadline"] <= time.time():
                self.store.finish(row["id"], status="error", category="local_rejection", code="image_queue_timeout", error="生图排队超时", http_status=504)
            elif not submitted and len(json.loads(row["attempted_accounts"])) >= 3:
                self.store.finish(row["id"], status="error", category=row["error_category"] or "platform_error", code=row["error_code"] or "image_attempts_exhausted",
                                  error=row["error"] or "生图账号尝试次数已用尽", http_status=row["http_status"] or 502)
            else:
                by_owner.setdefault(owner, []).append(row)
        with self._lock:
            if self._stop.is_set():
                return
            if self._thread:
                self._resize_pool(settings["global_concurrency"])
            for owner in by_owner:
                if owner not in self._owners:
                    self._owners.append(owner)
            active_counts = Counter(self._active.values())
            if len(self._owners) > 1 and self._owners[0] == self._last_owner:
                self._owners.rotate(-1)
            misses = 0
            while by_owner and len(self._active) < settings["global_concurrency"] and misses < len(self._owners):
                owner = self._owners[0]
                self._owners.rotate(-1)
                limit = self.store.quota(owner)["image_concurrency_limit"] or settings["key_concurrency"]
                if not by_owner.get(owner) or active_counts[owner] >= limit:
                    misses += 1
                    continue
                row = by_owner[owner][0]
                try:
                    reservation = self._reserve_account(row)
                except GenerationRuntimeError as exc:
                    submitted = row["recovery"] == "recovering_result"
                    if submitted:
                        self.recovery.finish_failed(row["id"], "image_result_unrecoverable", "原生图账号已删除或禁用，无法读取原结果")
                        self._log_task(row["id"], row["phase"])
                    else:
                        self.store.finish(row["id"], status="error",
                                      category=row["error_category"] or classify_image_error(exc).category,
                                      code=row["error_code"] or exc.code, error=row["error"] or str(exc), http_status=row["http_status"] or exc.status_code)
                    by_owner[owner].pop(0)
                    misses = 0
                    continue
                if reservation is None:
                    # This Key's head stays queued; other Keys can use eligible accounts.
                    misses += 1
                    continue
                pool, account_token = reservation
                by_owner[owner].pop(0)
                handed_off = False
                claimed = False
                try:
                    account = pool.get_account(account_token) if pool and account_token else None
                    if account_token and (not account or account.get("status") == "禁用"):
                        continue
                    if self.store.claim(row["id"], account_id=account.get("pool_account_id") if account else None,
                                        attempt=len(json.loads(row["attempted_accounts"])) + 1 if account and row["recovery"] != "recovering_result" else None):
                        claimed = True
                        self._active[row["id"]] = owner
                        self._last_owner = owner
                        active_counts[owner] += 1
                        self._pool.submit(self._run, row["id"], pool, account_token)
                        handed_off = True
                except Exception:
                    if claimed:
                        self._active.pop(row["id"], None)
                        active_counts[owner] -= 1
                        with self.store.lock:
                            self.store.db.execute("UPDATE tasks SET status='queued',phase=?,started=?,account_id=?,attempt=?,recovery=?,recovery_attempts=?,recovery_next_at=?,updated=? WHERE id=? AND status='running'",
                                                  (row["phase"], row["started"], row["account_id"], row["attempt"], row["recovery"], row["recovery_attempts"], row["recovery_next_at"], time.time(), row["id"]))
                    raise
                finally:
                    if not handed_off and account_token:
                        pool.release_image_slot(account_token)
                misses = 0
            self._owners = deque(owner for owner in self._owners if by_owner.get(owner) or active_counts[owner])

    def _reserve_account(self, row: dict):
        if not self._requires_account or row["phase"] in {"raw_saved", "output_saved"}:
            return None, ""
        pool = self.account_pool
        if pool is None:
            from services.protocol.conversation import account_service
            pool = account_service
        recovering = row["recovery"] == "recovering_result"
        if recovering and not row["account_id"]:
            raise GenerationRuntimeError("原生图账号标识缺失", "image_result_unrecoverable", 502)
        token = pool.try_acquire_image_token(model=row["model"], channel=row["channel"],
                                            excluded_ids=set() if recovering else set(json.loads(row["attempted_accounts"])),
                                            preferred_id=(row["account_id"] or "") if recovering else "")
        return (pool, token) if token else None

    def _run(self, task_id: str, account_pool=None, account_token: str = "") -> None:
        row = self.store.task(task_id, include_output=False)
        row["_account_token"] = account_token
        row["_account_pool"] = account_pool
        checkpoint = ExecutionCheckpoint(
            persist=lambda **values: self._persist_checkpoint(task_id, **values),
            raw_writer=lambda items: self._write_json(row["job_id"], f"{task_id}-raw.json", items),
            phase=row["phase"], conversation_id=row["conversation_id"] or "", account_id=row["account_id"] or "")
        token = execution_checkpoint.set(checkpoint)
        try:
            from services.protocol.conversation import ConversationRequest, ImageOutput
            payload = json.loads(row["payload"])
            payload.pop("client_task_ids", None)
            if payload.pop("has_inputs", False):
                payload.update(json.loads(self._private_path(row["job_id"], "inputs.json").read_text(encoding="utf-8")))
            request = ConversationRequest(**payload)
            request.progress_callback = lambda phase: self.store.checkpoint(task_id, progress=phase)
            if row["phase"] == "output_saved":
                serialized = json.loads(self._private_path(row["job_id"], f"{task_id}-output.json").read_text(encoding="utf-8"))
                if not isinstance(serialized, list) or not any(isinstance(item, dict) and item.get("kind") == "result" and item.get("data") for item in serialized):
                    raise GenerationRuntimeError("保存的图片结果不可读取", "image_result_checkpoint_invalid", 502)
            else:
                if row["phase"] == "raw_saved":
                    row["raw_images"] = json.loads(self._private_path(row["job_id"], f"{task_id}-raw.json").read_text(encoding="utf-8"))
                outputs = self.execute(request, row["ordinal"], request.n, row)
                serialized = [asdict(output) if isinstance(output, ImageOutput) else output for output in outputs]
                if not any(item.get("kind") == "result" and item.get("data") for item in serialized):
                    raise GenerationRuntimeError("上游未返回可读取图片", "no_image_generated", 502)
                self._write_json(row["job_id"], f"{task_id}-output.json", serialized)
                checkpoint.update("output_saved")
            if row["recovery_deadline"] is not None and time.time() >= row["recovery_deadline"]:
                self.recovery.finish_failed(task_id, "image_result_recovery_expired", "自动读取原结果已超过 10 分钟")
            else:
                self.store.finish(task_id, status="success", output=serialized)
                self._cleanup_conversation(self.store.task(task_id, include_output=False))
        except RetryImage:
            self.store.requeue_attempt(task_id)
        except Exception as exc:
            from services.protocol.conversation import public_image_error_message
            failure = classify_image_error(exc)
            current = self.store.task(task_id, include_output=False)
            uncertain = getattr(exc, "submission_uncertain", current["phase"] not in {"queued", "preparing", "account_selected", "rejected"})
            message = public_image_error_message(str(exc))
            self.store.finish(task_id, status="uncertain" if uncertain else "error", category=getattr(exc, "failure_category", failure.category),
                                  code="image_result_uncertain" if uncertain else (getattr(exc, "code", None) or failure.category),
                                  error=message, http_status=409 if uncertain else (getattr(exc, "status_code", None) or 502))
        finally:
            self._log_task(task_id, checkpoint.phase)
            execution_checkpoint.reset(token)
            if account_token:
                account_pool.release_image_slot(account_token)
            with self._lock:
                self._active.pop(task_id, None)
            self._wake.set()

    def _persist_checkpoint(self, task_id: str, **values) -> None:
        if values.get("phase") == "submitting":
            row = self.store.task(task_id, include_output=False)
            identity = self.identity_resolver(row["owner"])
            if not identity or not identity.get("enabled", True):
                self.store.finish(task_id, status="cancelled", category="local_rejection", code="image_key_disabled", error="API Key 已禁用或删除", http_status=403)
                raise GenerationRuntimeError("API Key 已禁用或删除", "image_key_disabled", 403)
            if row["reservation"] < 1:
                raise GenerationRuntimeError("任务额度预占不存在", "image_quota_reservation_missing", 409)
        self.store.checkpoint(task_id, **values)

    def _cleanup_conversation(self, row: dict) -> None:
        if not row["conversation_id"]:
            return
        from services.config import config
        if not (config.image_remove_conversation_after_result or config.image_remove_conversation_always):
            return
        try:
            from services.account_service import account_service
            from services.protocol.conversation import OpenAIBackendAPI, _remove_image_conversation_later
            account = next((item for item in account_service.list_accounts() if item.get("pool_account_id") == row["account_id"]), None)
            if account:
                backend = OpenAIBackendAPI(access_token=account["access_token"])
                try:
                    _remove_image_conversation_later(backend, row["conversation_id"], success=True)
                finally:
                    backend.close()
        except Exception:
            pass

    def _log_task(self, task_id: str, phase: str) -> None:
        try:
            from services.log_service import log_service, LOG_TYPE_CALL, sanitize_request_parameters
            from services.image_call_logging import task_diagnostics
            from utils.log import logger
            row = self.store.task(task_id, include_output=False)
            diagnostics = task_diagnostics(row, phase)
            logger.info({"event": "image_task_status", "image_task_id": row["job_id"], **diagnostics})
            # Compatible API calls are logged once by LoggedCall, including SSE.
            # Web background tasks have no waiting HTTP call to log their result.
            if not row["endpoint"].startswith("/api/image-tasks/") or row["status"] not in TERMINAL:
                return
            identity = json.loads(row["identity"])
            payload = json.loads(row["payload"])
            request_params = {key: value for key, value in payload.items() if key not in {"base_url", "client_task_ids", "has_inputs"}}
            log_service.add(LOG_TYPE_CALL, "生图任务状态", {"image_task_id": row["job_id"], "image_child_id": task_id,
                            "key_id": row["owner"], "key_name": identity.get("name", ""), "role": identity.get("role", ""),
                            "endpoint": row["endpoint"], "model": row["model"], "channel": row["channel"], "status": row["status"],
                            **diagnostics,
                            "request_params": sanitize_request_parameters(request_params)})
        except Exception:
            pass

    def outputs(self, task_id: str, request):
        from services.protocol.conversation import ImageOutput, ImageGenerationError
        emitted = set()
        last_progress = None
        while True:
            rows = self.store.rows("SELECT id,status,phase,recovery,error,error_code,http_status,conversation_id,account_id FROM tasks WHERE job_id=? ORDER BY ordinal", (task_id,))
            with self._lock:
                active = set(self._active)
            for row in rows:
                if row["status"] == "success" and row["id"] not in emitted and row["id"] not in active:
                    values = self.store.output(row["id"])
                    if values is None:
                        raise ImageGenerationError("图片结果正文已超过保留期限", code="image_result_expired", status_code=410)
                    emitted.add(row["id"])
                    for value in values:
                        yield ImageOutput(**value)
                    del values
            unfinished = [row for row in rows if row["status"] in {"queued", "running", "uncertain"} or row["id"] in active]
            if not unfinished:
                if not emitted:
                    failed = rows[0]
                    error = ImageGenerationError(failed["error"] or "生图未完成", code=failed["error_code"] or "image_generation_failed", status_code=failed["http_status"] or 502,
                                                 conversation_id=failed["conversation_id"] or "", upstream_status=failed["http_status"] if str(failed["error_code"]).startswith("upstream_") else None)
                    error.pool_account_id = failed["account_id"] or ""
                    raise error
                return
            phase = "recovering_result" if any(row["recovery"] in {"auto_pending", "recovering_result"} or row["status"] == "uncertain" for row in unfinished) else ("queued" if all(row["status"] == "queued" for row in unfinished) else "generating")
            if phase != last_progress:
                yield ImageOutput(kind="progress", model=request.model, index=1, total=request.n, text=phase, upstream_event_type=phase)
                if request.progress_callback:
                    request.progress_callback(phase)
                last_progress = phase
            self._wake.wait(0.25)

    def jobs(self, identity: dict, ids: list[str] | None = None, *, all_users: bool = False) -> list[dict]:
        query = "SELECT * FROM jobs"
        params = []
        if not all_users:
            query += " WHERE owner=?"
            params.append(str(identity["id"]))
        query += " ORDER BY created DESC" + (" LIMIT 1000" if not ids else "")
        rows = self.store.rows(query, params)
        result = []
        for row in rows:
            client_ids = json.loads(row["payload"]).get("client_task_ids") or []
            if not ids or row["id"] in ids or row["external_id"] in ids or any(key in ids for key in client_ids):
                children = self.store.public_tasks(row["id"])
                item = self.public_job(row, children)
                if client_ids and not all_users:
                    for index, child in enumerate(children):
                        if ids and client_ids[index] not in ids and row["id"] not in ids:
                            continue
                        data = json.loads(child["image_data"] or "[]")
                        result.append({**item, "id": client_ids[index], "status": child["status"], "phase": child["phase"], "progress": child["phase"],
                                       "data": [{k: v for k, v in image.items() if k != "b64_json"} for image in data], "error": child["error"],
                                       "error_category": child["error_category"], "error_code": child["error_code"], "recovery_status": child["recovery"], "conversation_id": child["conversation_id"],
                                       "queue_seconds": round((child["started"] or child["finished"] or time.time()) - child["created"], 2),
                                       "updated_at": _iso(child["updated"]),
                                       **recovery_details(child),
                                       "recovery_active": child["status"] == "uncertain" or (child["status"] in {"queued", "running"} and child["recovery"] == "recovering_result"),
                                       "children": [item["children"][index]]})
                else:
                    result.append(item)
        return result

    def public_job(self, job: dict, rows: list[dict] | None = None) -> dict:
        rows = rows if rows is not None else self.store.public_tasks(job["id"])
        counts = Counter(row["status"] for row in rows)
        status = next((s for s in ("running", "queued", "uncertain") if counts[s]), "success" if counts["success"] else ("cancelled" if counts["cancelled"] else "error"))
        payload = json.loads(job["payload"])
        data = [item for row in rows if row["status"] == "success" for item in json.loads(row["image_data"] or "[]")]
        current = next((row for row in rows if row["status"] == status), rows[0])
        recovering = [row for row in rows if row["status"] == "uncertain" or (row["status"] in {"queued", "running"} and row["recovery"] == "recovering_result")]
        current = next(iter(recovering), next((row for row in rows if row["recovery_deadline"] is not None or row["recovery"] == "auto_failed"), current))
        started = min((r["started"] for r in rows if r["started"]), default=None)
        return {"id": job["external_id"] if job["endpoint"].startswith("/api/image-tasks") else job["id"],
                "task_id": job["id"], "status": status, "mode": "edit" if "edit" in job["endpoint"] else "generate",
                "model": job["model"], "channel": job["channel"], "key_id": job["owner"], "size": payload.get("size"), "quality": payload.get("quality", "auto"),
                "created_at": _iso(job["created"]), "updated_at": _iso(max(r["updated"] for r in rows)), "data": data,
                "phase": current["phase"], "progress": current["phase"], "recovery_status": current["recovery"],
                "error_category": current["error_category"], "error_code": current["error_code"], "error": current["error"],
                "conversation_id": current["conversation_id"], "queue_seconds": round((started or current["finished"] or time.time()) - job["created"], 2),
                "elapsed_secs": round(time.time() - (started or job["created"]), 1) if status in {"queued", "running"} else None,
                "partial_success": bool(counts["success"] and counts["success"] < len(rows)),
                **recovery_details(current),
                "recovery_active": bool(recovering),
                "children": [{**{k: r[k] for k in ("id", "ordinal", "status", "phase", "recovery", "error_category", "error_code", "attempt")}, **recovery_details(r)} for r in rows]}

    def change_task(self, identity: dict, task_id: str, action: str) -> dict:
        jobs = self.jobs(identity, [task_id], all_users=identity.get("role") == "admin")
        if not jobs:
            raise GenerationRuntimeError("任务不存在", "image_task_not_found", 404)
        job = jobs[0]
        if action == "end" and identity.get("role") != "admin":
            raise GenerationRuntimeError("需要管理员权限", "forbidden", 403)
        with self._lock:
            client_ids = json.loads(self.store.rows("SELECT payload FROM jobs WHERE id=?", (job["task_id"],))[0]["payload"]).get("client_task_ids") or []
            for child in job["children"]:
                if task_id in client_ids and child["ordinal"] != client_ids.index(task_id) + 1:
                    continue
                row = self.store.task(child["id"], include_output=False)
                if action == "cancel" and row["status"] == "queued" and row["recovery"] != "recovering_result":
                    self.store.finish(row["id"], status="cancelled", code="image_task_cancelled", error="任务已取消", http_status=409)
                elif action == "end" and row["status"] == "uncertain":
                    self.store.finish(row["id"], status="cancelled", code="image_task_ended", error="管理员已结束待确认任务", http_status=409)
                elif action == "resume":
                    # Legacy clients may request recovery; the scheduler owns its
                    # deadlines and attempts, so repeated clicks cannot bypass them.
                    if row["status"] == "uncertain" or row["recovery"] in {"auto_pending", "recovering_result"}:
                        continue
                    if row["status"] != "success":
                        raise GenerationRuntimeError("任务已结束，无法继续读取原结果", "image_task_ended", 409)
        self._wake.set()
        return self.jobs(identity, [job["task_id"]], all_users=identity.get("role") == "admin")[0]

    def cleanup(self) -> None:
        import shutil
        cutoff = time.time() - self.settings_getter()["task_retention_days"] * 86400
        rows = self.store.rows("SELECT id FROM jobs WHERE created<? AND NOT EXISTS (SELECT 1 FROM tasks WHERE job_id=jobs.id "
                               "AND (status IN ('queued','running','uncertain') OR COALESCE(finished,updated)>=?))", (cutoff, cutoff))
        for row in rows:
            if not self.store.expire_outputs(row["id"], cutoff):
                continue
            directory = self.directory / "generation-tasks" / row["id"]
            if directory.is_dir() and directory.resolve().parent == (self.directory / "generation-tasks").resolve():
                shutil.rmtree(directory)

    def _import_legacy(self) -> None:
        if self.store.rows("SELECT * FROM metadata WHERE name='legacy_imported'"):
            return
        path = self.directory / "image_tasks.json"
        if path.exists():
            from services.generation_migration import import_legacy_tasks
            import_legacy_tasks(self.store, path)
        with self.store.lock:
            self.store.db.execute("INSERT OR IGNORE INTO metadata VALUES('legacy_imported','1')")


_runtime: GenerationRuntime | None = None
_runtime_lock = threading.Lock()


def get_generation_runtime() -> GenerationRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            from services.config import DATA_DIR, config
            _runtime = GenerationRuntime(DATA_DIR, settings_getter=lambda: queue_settings(config.data.get("image_queue")))
        return _runtime
