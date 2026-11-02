import assert from "node:assert/strict";
import { test } from "node:test";
import { record, items, chatStream } from "./native-media";

test("未知外部数据不会作为模型数组使用", () => {
  assert.deepEqual(record(null), {});
  assert.deepEqual(items({ items: "bad" }), []);
  assert.deepEqual(items({ data: [{ id: "grok-chat-fast" }] }, "data"), [{ id: "grok-chat-fast" }]);
});

test("SSE 跨网络分块保留中文，并传播流内错误", async () => {
  const originalFetch = globalThis.fetch;
  const encoder = new TextEncoder();
  const bytes = encoder.encode('data: {"choices":[{"delta":{"content":"你好"}}]}\n\ndata: {"error":{"message":"上游中断"}}\n\n');
  let cancelled = false;
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) { for (let i = 0; i < bytes.length; i += 7) controller.enqueue(bytes.slice(i, i+7)); },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "text/event-stream" } });
  try {
    const chunks: string[] = [];
    await assert.rejects(async () => { for await (const text of chatStream({}, new AbortController().signal)) chunks.push(text); }, /上游中断/);
    assert.deepEqual(chunks, ["你好"]);
    assert.equal(cancelled, true);
  } finally { globalThis.fetch = originalFetch; }
});
