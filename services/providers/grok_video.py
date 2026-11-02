"""Durable video jobs; interrupted submissions are never blindly replayed."""
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from services.providers.grok_accounts import pool
from services.providers.grok_store import get_store
from services.providers.grok_web import GrokError, GrokWeb


def validate(body):
    if body.get("model", "grok-imagine-video") != "grok-imagine-video":
        raise HTTPException(400, "视频仅支持 grok-imagine-video")
    if any(body.get(k) for k in ("image", "image_url", "reference_urls", "video", "video_url")):
        raise HTTPException(400, "Grok Web 当前仅支持文生视频")
    prompt, duration = body.get("prompt", ""), body.get("duration", 6)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise HTTPException(400, "视频提示词不能为空且不得超过 32000 字符")
    if type(duration) is not int or not 1 <= duration <= 15:
        raise HTTPException(400, "duration 必须是 1–15 秒的整数")
    ratio, resolution = body.get("aspect_ratio", "16:9"), body.get("resolution", "720p")
    if ratio not in {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"} or resolution not in {"480p", "720p"}:
        raise HTTPException(400, "视频比例或分辨率不受支持")
    return {"model": "grok-imagine-video", "prompt": prompt.strip(), "duration": duration,
            "aspect_ratio": ratio, "resolution": resolution}


def public(job):
    return {k: v for k, v in job.items() if k not in {"owner", "identity", "fingerprint", "idempotency_key", "account_id", "remote_url"}}


class VideoRuntime:
    def __init__(self):
        self.lock = threading.RLock()
        self.executor = None
        self.active = set()

    def start(self):
        with self.lock:
            if self.executor is not None:
                return
            store = get_store()
            store.cleanup()
            self.executor = ThreadPoolExecutor(max_workers=store.settings()["global_concurrency"], thread_name_prefix="grok-video")
            for job in store.list("video", limit=None):
                if job["status"] in {"queued", "in_progress"}:
                    if job.get("remote_url") or job["status"] == "queued":
                        self._schedule(job["id"])
                    else:
                        job.update(status="uncertain", error="服务重启，原视频提交结果待确认；不会自动重复生成")
                        store.put("video", job["id"], job["owner"], job)

    def stop(self):
        with self.lock:
            executor, self.executor = self.executor, None
        if executor:
            executor.shutdown(wait=True)

    def submit(self, identity, body, idempotency_key=""):
        body = validate(body)
        if len(idempotency_key) > 256:
            raise HTTPException(400, "Idempotency-Key 太长")
        from services.content_filter import check_request
        check_request(body["prompt"])
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        self.start()
        with self.lock:
            store = get_store()
            old = store.video_by_key(identity["id"], idempotency_key) if idempotency_key else None
            if old:
                if old["fingerprint"] != fingerprint:
                    raise HTTPException(409, "相同 Idempotency-Key 不能用于不同请求")
                return public(old)
            if len(self.active) >= 200:
                raise HTTPException(429, "视频队列已满")
            job = {**body, "id": "video_" + uuid.uuid4().hex, "object": "video", "owner": identity["id"],
                   "identity": identity, "status": "queued", "progress": 0, "created_at": int(time.time()),
                   "fingerprint": fingerprint, "idempotency_key": idempotency_key, "error": None}
            store.put("video", job["id"], identity["id"], job)
            self._schedule(job["id"])
            return public(job)

    def _schedule(self, job_id):
        if job_id not in self.active:
            self.active.add(job_id)
            self.executor.submit(self._run, job_id)

    def retry_download(self, owner, job_id):
        self.start()
        with self.lock:
            job = get_store().get("video", job_id, owner)
            if job_id in self.active or not job.get("remote_url") or job["status"] != "uncertain":
                raise HTTPException(409, "此任务不能重试下载；生成结果未知时不会重复提交")
            job.update(status="in_progress", error=None)
            get_store().put("video", job_id, owner, job)
            self._schedule(job_id)
            return public(job)

    def _run(self, job_id):
        store = get_store()
        job = store.get("video", job_id)
        from services.log_service import LoggedCall
        call = LoggedCall(job["identity"], "/v1/videos/generations", job["model"], "视频生成", request_text=job["prompt"])
        client = None
        try:
            from services.generation_runtime import GenerationRuntime
            identity = GenerationRuntime._resolve_identity(job["owner"])
            if not identity or not identity.get("enabled", True):
                raise HTTPException(403, "用户密钥已禁用或删除")
            with pool.lease(job["model"], job["owner"], job.get("account_id", "")) as account:
                job.update(account_id=account["id"], status="in_progress")
                store.put("video", job_id, job["owner"], job)
                with GrokWeb(account) as client:
                    if not job.get("remote_url"):
                        def progress(value):
                            job["progress"] = value
                            store.put("video", job_id, job["owner"], job)
                        url, actual_duration = client.video(job, progress)
                        job.update(remote_url=url, actual_duration=actual_duration)
                        store.put("video", job_id, job["owner"], job)
                    directory = store.directory / "videos"
                    directory.mkdir(exist_ok=True)
                    partial = directory / (job_id + ".part")
                    with partial.open("wb") as f:
                        for chunk in client.download(job["remote_url"], limit=512 * 1024 * 1024):
                            f.write(chunk)
                    with partial.open("rb") as f:
                        header = f.read(12)
                    if len(header) < 12 or header[4:8] != b"ftyp":
                        raise GrokError("Grok 视频文件不完整", submitted=True)
                    partial.replace(directory / (job_id + ".mp4"))
                job.update(status="completed", progress=100, completed_at=int(time.time()))
                call.log("调用完成", {"video_id": job_id, "duration": job.get("actual_duration")})
        except Exception as exc:
            uncertain = bool(job.get("remote_url")) or getattr(exc, "submission_uncertain", bool(client and client.submitted))
            message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc) if isinstance(exc, GrokError) else "视频处理失败"
            job.update(status="uncertain" if uncertain else "failed", error=message)
            call.log("调用失败", status="failed", error=message)
        finally:
            store.put("video", job_id, job["owner"], job)
            with self.lock:
                self.active.discard(job_id)


runtime = VideoRuntime()
