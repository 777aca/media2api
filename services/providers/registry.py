"""Dispatch by explicit model namespace, preserving legacy default routing."""
from services.providers.grok_catalog import is_grok


def conversation_handler(operation, legacy):
    def handle(body):
        if is_grok(body.get("model")):
            from services.providers.grok_protocol import handle as grok_handle
            return grok_handle(body, operation)
        return legacy(body)
    return handle


def execute_grok_image(request, index, total, row):
    import base64
    from services.generation_context import checkpoint
    from services.protocol.conversation import ImageOutput, format_image_result
    from services.providers.grok_store import get_store
    from services.providers.grok_web import GrokWeb
    if row.get("raw_images"):
        items = row["raw_images"]
    else:
        account_id = row["_account_token"]
        checkpoint("account_selected", account_id=account_id)
        with GrokWeb(get_store().account(account_id, secret=True)) as client:
            raw = client.image(request)
        items = [{"b64_json": base64.b64encode(raw).decode("ascii")}]
    result = format_image_result(items, request.prompt, request.response_format, request.base_url,
                                 requested_size=request.size, progress_callback=request.progress_callback, max_images=1)
    return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=result["data"])]
