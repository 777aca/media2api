"""OpenAI/Anthropic representations over the native Web conversation stream."""
from __future__ import annotations

import html
import json
import re
import time
import uuid

from fastapi import HTTPException

from services.providers.grok_accounts import pool
from services.providers.grok_catalog import resolve
from services.providers.grok_store import get_store
from services.providers.grok_web import GrokError, GrokWeb


def normalize(body, operation):
    messages = body.get("messages") if operation != "responses" else body.get("input")
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if messages is None and body.get("prompt"):
        messages = [{"role": "user", "content": body["prompt"]}]
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "对话输入不能为空")
    parts, attachments = [], []
    instructions = body.get("instructions") or body.get("system")
    if instructions:
        parts.append("system: " + (instructions if isinstance(instructions, str) else json.dumps(instructions, ensure_ascii=False)))
    for m in messages:
        if not isinstance(m, dict):
            raise HTTPException(400, "消息格式无效")
        content = m.get("content", m.get("output", ""))
        if m.get("type") in {"function_call", "function_call_output"}:
            content = json.dumps(m, ensure_ascii=False)
        if isinstance(content, list):
            texts = []
            for block in content:
                if not isinstance(block, dict):
                    raise HTTPException(400, "消息内容格式无效")
                kind = block.get("type")
                if kind in {"text", "input_text", "output_text"}:
                    texts.append(str(block.get("text", "")))
                elif kind in {"image_url", "input_image", "image"}:
                    image = block.get("image_url", "")
                    if isinstance(image, dict):
                        image = image.get("url", "")
                    if kind == "image":
                        src = block.get("source", {})
                        image = "data:" + str(src.get("media_type", "image/png")) + ";base64," + str(src.get("data", ""))
                    if not isinstance(image, str) or not image.startswith("data:image/"):
                        raise HTTPException(400, "Grok 对话图片请使用 base64 data URL")
                    attachments.append(image)
                elif kind in {"tool_result", "tool_use"}:
                    texts.append(json.dumps(block, ensure_ascii=False))
                else:
                    raise HTTPException(400, "Grok 不支持该消息内容类型")
            content = "\n".join(texts)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        parts.append(str(m.get("role", m.get("type", "user"))) + ": " + content)
        if m.get("tool_calls"):
            parts.append(json.dumps(m["tool_calls"], ensure_ascii=False))
    if len(attachments) > 8:
        raise HTTPException(400, "每次对话最多上传 8 张图片")
    tools = []
    if body.get("tools") is not None and not isinstance(body["tools"], list):
        raise HTTPException(400, "tools 必须是数组")
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            raise HTTPException(400, "tools 格式无效")
        if t.get("type") in {"web_search", "web_search_preview"}:
            continue
        if t.get("type") not in {None, "function"}:
            raise HTTPException(400, "Grok 仅支持函数工具和网页搜索工具")
        f = t.get("function", t)
        if not isinstance(f, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(f.get("name", ""))):
            raise HTTPException(400, "工具名称无效")
        tools.append({"name": f["name"], "description": f.get("description", ""),
                      "parameters": f.get("parameters", f.get("input_schema", {}))})
    if len(tools) > 128 or len({t["name"] for t in tools}) != len(tools):
        raise HTTPException(400, "工具名称不能重复，最多 128 个")
    choice = body.get("tool_choice") or "auto"
    forced = ""
    if isinstance(choice, dict):
        function = choice.get("function", choice)
        if not isinstance(function, dict):
            raise HTTPException(400, "tool_choice 格式无效")
        forced = function.get("name", "")
        choice = choice.get("type", "auto")
        if choice == "any":
            choice = "required"
    if not isinstance(choice, str) or choice not in {"auto", "none", "required", "function", "tool"}:
        raise HTTPException(400, "tool_choice 无效")
    if forced and forced not in {t["name"] for t in tools}:
        raise HTTPException(400, "指定工具不存在")
    if choice == "none":
        tools = []
    if tools:
        parts.insert(0, "Available functions: " + json.dumps(tools, ensure_ascii=False) +
            '\nTo call a function, emit <tool_calls><tool_call><tool_name>NAME</tool_name><parameters>{JSON object}</parameters></tool_call></tool_calls>. Do not execute functions yourself.' +
            (" You must call " + (forced or "a function") + "." if forced or choice == "required" else ""))
    elif choice == "required":
        raise HTTPException(400, "没有可用函数工具")
    return "\n\n".join(parts), attachments, tools, forced, choice


def parse_calls(text, tools):
    calls = []
    names = {t["name"] for t in tools}
    pattern = r"<tool_calls\s*>(.*?)</tool_calls\s*>"
    root = re.search(pattern, text, re.S | re.I)
    if not root:
        return text, calls
    for raw in re.findall(r"<tool_call\s*>(.*?)</tool_call\s*>", root[1], re.S | re.I):
        name = re.search(r"<tool_name>(.*?)</tool_name>", raw, re.S)
        params = re.search(r"<parameters>(.*?)</parameters>", raw, re.S)
        name = html.unescape(name[1].strip()) if name else ""
        if name not in names:
            raise GrokError("Grok 返回未声明的工具")
        try:
            arguments = json.loads(html.unescape(params[1]) if params else "{}")
            if not isinstance(arguments, dict):
                raise ValueError()
        except ValueError as exc:
            raise GrokError("Grok 返回无效工具参数") from exc
        calls.append({"id": "call_" + uuid.uuid4().hex, "type": "function",
                      "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}})
    return (text[:root.start()] + text[root.end():]).strip(), calls


def handle(body, operation="chat"):
    spec = resolve(body.get("model"))
    if spec.capability != "chat":
        raise HTTPException(400, "该模型不支持文本对话，请使用对应媒体接口")
    owner = str(body["_identity"]["id"])
    prompt, attachments, tools, forced, choice = normalize(body, operation)
    previous = None
    if body.get("previous_response_id"):
        previous = get_store().get("response", str(body["previous_response_id"]), owner)
        if previous.get("expires_at", 0) < time.time() or previous.get("model") != spec.id:
            raise HTTPException(400, "原响应已过期或模型不一致")
    result = _generate(body, operation, spec.id, owner, prompt, attachments, tools, forced, choice, previous)
    if body.get("stream"):
        return result
    final = None
    for event in result:
        if "_result" in event:
            final = event["_result"]
    return final


def _generate(body, operation, model, owner, prompt, attachments, tools, forced, choice, previous):
    response_id = "resp_" + uuid.uuid4().hex
    msg_id = "msg_" + uuid.uuid4().hex
    created = int(time.time())
    streaming = bool(body.get("stream"))
    text, reasoning, conversation_id, parent_id = "", "", "", ""
    with pool.lease(model, owner, (previous or {}).get("account_id", "")) as account:
        with GrokWeb(account) as client:
            if streaming:
                if operation == "responses":
                    yield {"type": "response.created", "response": {"id": response_id, "object": "response", "status": "in_progress", "model": model, "output": []}}
                    yield {"type": "response.output_item.added", "output_index": 0, "item": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "status": "in_progress"}}
                    yield {"type": "response.content_part.added", "item_id": msg_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}
                elif operation == "messages":
                    yield {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "usage": {"input_tokens": max(1, len(prompt)//4), "output_tokens": 0}}}
                    yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
            for event in client.chat_events(prompt, model, attachments, previous):
                conversation_id = event.get("conversation_id", conversation_id)
                kind, delta = event.get("type"), ""
                if kind == "response.chunk":
                    chunk = event.get("chunk", {}).get("text", {})
                    channel = chunk.get("channel", "")
                    if channel in {"", "CHANNEL_ASSISTANT_RESPONSE"}:
                        delta = chunk.get("text", "")
                    else:
                        reasoning += str(chunk.get("text", ""))
                elif kind == "response.output_text.delta":
                    delta = event.get("delta", "")
                elif kind == "response.output_text.done" and not text:
                    delta = event.get("text", "")
                elif kind == "response.done":
                    parent_id = event.get("response", {}).get("id", "")
                if not isinstance(delta, str):
                    raise GrokError("Grok 文本事件无效")
                text += delta
                if len(text) + len(reasoning) > 8 * 1024 * 1024:
                    raise GrokError("Grok 对话响应过大")
                if streaming and delta and not tools:
                    yield _delta(operation, response_id, msg_id, model, created, delta)
            text, calls = parse_calls(text, tools) if tools else (text, [])
            if (choice == "required" and not calls) or (forced and not any(c["function"]["name"] == forced for c in calls)):
                raise GrokError("Grok 没有返回要求的工具调用")
            usage = {"prompt_tokens": max(1, len(prompt)//4), "completion_tokens": max(0, (len(text)+len(reasoning))//4), "estimated": True}
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            message = {"role": "assistant", "content": text}
            if reasoning:
                message["reasoning_content"] = reasoning
            if calls:
                message["tool_calls"] = calls
            result = {"id": response_id, "object": "chat.completion", "created": created, "model": model,
                      "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}], "usage": usage}
            if operation == "responses":
                output = [{"id": msg_id, "type": "message", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text, "annotations": []}]}]
                output.extend({"id": c["id"], "type": "function_call", "call_id": c["id"], "status": "completed", **c["function"]} for c in calls)
                result = {"id": response_id, "object": "response", "created_at": created, "model": model, "status": "completed", "output": output,
                          "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"], "total_tokens": usage["total_tokens"], "estimated": True}}
            elif operation == "messages":
                content = [{"type": "text", "text": text}] if text else []
                content.extend({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": json.loads(c["function"]["arguments"])} for c in calls)
                result = {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": content,
                          "stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None,
                          "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"], "estimated": True}}
            if operation == "responses" and body.get("store", True):
                get_store().put("response", response_id, owner, {"id": response_id, "account_id": account["id"],
                    "model": model, "conversation_id": conversation_id, "parent_id": parent_id, "result": result,
                    "expires_at": time.time() + get_store().settings()["retention_days"] * 86400})
            if not streaming:
                yield {"_result": result}
                return
            if tools and text:
                yield _delta(operation, response_id, msg_id, model, created, text)
            if operation == "chat":
                if calls:
                    yield {"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                           "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, **c} for i,c in enumerate(calls)]}, "finish_reason": None}]}
                yield {"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}], "usage": usage}
            elif operation == "responses":
                yield {"type": "response.output_text.done", "item_id": msg_id, "output_index": 0, "content_index": 0, "text": text}
                yield {"type": "response.content_part.done", "item_id": msg_id, "output_index": 0, "content_index": 0, "part": result["output"][0]["content"][0]}
                for i, item in enumerate(result["output"]):
                    if i:
                        yield {"type": "response.output_item.added", "output_index": i, "item": {**item, "arguments": "", "status": "in_progress"}}
                        yield {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": i, "delta": item["arguments"]}
                        yield {"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": i, "arguments": item["arguments"]}
                    yield {"type": "response.output_item.done", "output_index": i, "item": item}
                yield {"type": "response.completed", "response": result}
            else:
                yield {"type": "content_block_stop", "index": 0}
                for i, c in enumerate(calls, 1):
                    yield {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": {}}}
                    yield {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": c["function"]["arguments"]}}
                    yield {"type": "content_block_stop", "index": i}
                yield {"type": "message_delta", "delta": {"stop_reason": result["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": usage["completion_tokens"]}}
                yield {"type": "message_stop"}


def _delta(operation, rid, mid, model, created, text):
    if operation == "responses":
        return {"type": "response.output_text.delta", "response_id": rid, "item_id": mid, "output_index": 0, "content_index": 0, "delta": text}
    if operation == "messages":
        return {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
    return {"id": rid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
