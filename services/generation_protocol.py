from __future__ import annotations

from services.generation_context import GenerationContext, PreparedImageRequest
from services.generation_errors import GenerationRuntimeError


def record_image_rejection(identity: dict, endpoint: str, body: dict, exc: Exception, *, category: str | None = None) -> None:
    """Record authenticated image refusals before admission exactly once per request."""
    if getattr(exc, "image_rejection_recorded", False) or not is_governed_image_call(endpoint, body):
        return
    from services.generation_errors import classify_image_error
    from services.generation_runtime import get_generation_runtime
    from utils.helper import is_codex_image_model
    model = str(body.get("model") or "gpt-image-2")
    failure = category or ("invalid_request" if isinstance(exc, ValueError) else classify_image_error(exc).category)
    get_generation_runtime().store.reject(identity, model, "codex" if is_codex_image_model(model) else "web", getattr(exc, "code", None) or failure, failure)
    exc.image_rejection_recorded = True


def prepare_image_call(handler, body: dict, context: GenerationContext) -> None:
    """Normalize with the existing protocol parser; stop before any upstream generation."""
    context.prepare_only = True
    try:
        result = handler({**body, "_image_context": context})
        if not isinstance(result, dict):
            for _ in result:
                pass
    except PreparedImageRequest:
        pass
    except Exception as exc:
        record_image_rejection(context.identity, context.endpoint, body, exc)
        if isinstance(exc, ValueError) and not hasattr(exc, "to_openai_error"):
            error = GenerationRuntimeError(str(exc), "invalid_image_request", 400)
            error.image_rejection_recorded = True
            raise error from exc
        raise
    finally:
        context.prepare_only = False
    if context.request is None:
        raise GenerationRuntimeError("请求未包含生图任务", "invalid_image_request", 400)
    from services.generation_runtime import get_generation_runtime
    try:
        get_generation_runtime().submit(context.request, context)
    except GenerationRuntimeError as exc:
        record_image_rejection(context.identity, context.endpoint, body, exc)
        raise


def is_governed_image_call(endpoint: str, body: dict) -> bool:
    if endpoint.startswith("/v1/images") or endpoint.startswith("/api/image-tasks"):
        return True
    if endpoint == "/v1/chat/completions":
        from utils.helper import is_image_chat_request
        return is_image_chat_request(body)
    if endpoint == "/v1/responses":
        from services.protocol.openai_v1_response import is_text_response_request
        return not is_text_response_request(body)
    return False


class DurableImageTasks:
    def submit_generation(self, identity: dict, **values) -> dict:
        from services.protocol.openai_v1_image_generations import handle
        return self._submit(identity, handle, "generations", values)

    def submit_edit(self, identity: dict, **values) -> dict:
        from services.protocol.openai_v1_image_edit import handle
        values["mask"] = values.pop("masks", None)
        return self._submit(identity, handle, "edits", values)

    def _submit(self, identity, handler, endpoint, values):
        from services.generation_runtime import get_generation_runtime
        values.pop("request_params", None)
        external_id = values.pop("client_task_id")
        context = GenerationContext(identity, f"/api/image-tasks/{endpoint}", external_id)
        context.client_task_ids = values.pop("client_task_ids", None) or []
        prepare_image_call(handler, {**values, "n": len(context.client_task_ids) or 1, "response_format": "url"}, context)
        items = get_generation_runtime().jobs(identity, [context.task_id])
        return {"items": items} if context.client_task_ids else items[0]

    def list_tasks(self, identity: dict, task_ids: list[str] | None = None) -> dict:
        from services.generation_runtime import get_generation_runtime
        runtime = get_generation_runtime()
        runtime.start()
        items = runtime.jobs(identity, task_ids)
        return {"items": items, "missing_ids": [key for key in (task_ids or []) if not any(item["id"] == key or item["task_id"] == key for item in items)]}

    def resume_poll(self, identity: dict, task_id: str, extra_timeout_secs: float = 30) -> dict:
        from services.generation_runtime import get_generation_runtime
        return get_generation_runtime().change_task(identity, task_id, "resume")
