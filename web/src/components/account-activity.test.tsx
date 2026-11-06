import { test } from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { AccountActivityBadge, createActivityPoller, mergeActivity } from "./account-activity";

test("activity polling skips blocked pages and overlapping requests, discards invalidated responses", async () => {
  let blocked = true;
  let calls = 0;
  let resolve!: (value: number) => void;
  const applied: number[] = [];
  const poller = createActivityPoller({
    canPoll: () => !blocked,
    fetch: () => { calls++; return new Promise<number>(done => { resolve = done; }); },
    apply: value => { applied.push(value); },
  });
  await poller.poll();
  assert.equal(calls, 0);
  blocked = false;
  const first = poller.poll();
  await poller.poll();
  assert.equal(calls, 1);
  poller.invalidate(); // account mutation or unmount while the read is pending
  resolve(2);
  await first;
  assert.deepEqual(applied, []);
  const second = poller.poll();
  resolve(0);
  await second;
  assert.deepEqual(applied, [0]);
});

test("poll errors retain activity and allow the next poll; hiding discards pending data", async () => {
  let hidden = false;
  let fail = true;
  const applied: number[] = [];
  const poller = createActivityPoller({
    canPoll: () => !hidden,
    fetch: async () => { if (fail) throw new Error("offline"); hidden = true; return 2; },
    apply: value => { applied.push(value); },
  });
  await poller.poll();
  fail = false;
  await poller.poll();
  assert.equal(hidden, true);
  assert.deepEqual(applied, []);
});

test("activity merge updates only counts, preserves mutations and never resurrects removed accounts", () => {
  const current = [{ id: "kept", name: "new name", count: 2 }, { id: "new", name: "new", count: 1 }];
  const result = mergeActivity(current, [
    { id: "kept", name: "old name", count: 0 }, { id: "deleted", name: "old", count: 3 },
  ], item => item.id, "count");
  assert.deepEqual(result, [{ id: "kept", name: "new name", count: 0 }, current[1]]);
  assert.equal(mergeActivity(result, result, item => item.id, "count"), result);
});

test("activity badges distinguish image counts from Grok requests without requiring motion", () => {
  const image = renderToStaticMarkup(<AccountActivityBadge count={2} />);
  const grok = renderToStaticMarkup(<AccountActivityBadge count={3} kind="request" />);
  assert.match(image, /生成中 · 2 张/);
  assert.match(grok, /处理中 · 3 个请求/);
  assert.doesNotMatch(grok, /张/);
  assert.match(image, /role="status"/);
  assert.match(image, /motion-safe:animate-spin/);
  assert.equal(renderToStaticMarkup(<AccountActivityBadge count={0} />), "");
});
