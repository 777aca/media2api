from fastapi import Request

from services.request_log import sanitize_request_parameters


async def read_request_parameters(request: Request) -> dict[str, object]:
    """复用接口已解析的请求缓存，保留原始字段及重复上传字段。"""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not content_type or content_type == "application/json" or content_type.endswith("+json"):
        return sanitize_request_parameters(await request.json())
    form = await request.form()
    parameters: dict[str, object] = {}
    for key in form:
        values = form.getlist(key)
        parameters[key] = values if len(values) > 1 else values[0]
    return sanitize_request_parameters(parameters)
