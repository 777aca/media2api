import { test } from "node:test";
import assert from "node:assert/strict";
import { filterPoolRows, grokStatus, mergeImageAccountActivity, parseGrokAccounts, partitionPoolRows, poolRows } from "./account-pool";
import type { Account } from "./api";

const openai: Account[] = [
  { access_token: "same-id", type: "Plus", email: "alice@example.test", status: "正常", quota: 4, success: 0, fail: 0 },
  { access_token: "codex-test", type: "Free", source_type: " CODEX ", email: "bob@example.test", status: "异常", quota: 0, success: 0, fail: 0 },
];

test("image activity polling applies quota, status and in-flight count from the same account snapshot", () => {
  const current = [{ ...openai[0], quota: 0, status: "限流" as const, image_inflight: 0 }, openai[1]];
  const fresh = { ...openai[0], image_inflight: 1, email: "stale@example.test", restore_at: null };
  const updated = mergeImageAccountActivity(current, [fresh, { ...fresh, access_token: "deleted-token" }]);
  assert.deepEqual(updated, [{ ...openai[0], image_inflight: 1, restore_at: null }, openai[1]]);
  assert.equal(updated[1], current[1]);

  const completed = { ...fresh, image_inflight: 0, quota: 0, status: "限流", restore_at: "2026-11-26T15:00:00Z" };
  const settled = mergeImageAccountActivity(updated, [completed]);
  assert.deepEqual(settled[0], { ...current[0], restore_at: completed.restore_at });
  assert.equal(mergeImageAccountActivity(settled, [completed]), settled);
  assert.equal(mergeImageAccountActivity(settled, [{ ...completed, image_inflight: -1 }]), settled);
});

test("Grok request counts accept only nonnegative integers and default old responses to zero", () => {
  for (const [value, expected] of [[3, 3], [0, 0], [undefined, 0], [-1, 0], [1.5, 0], ["2", 0], [Infinity, 0]] as const) {
    const [account] = parseGrokAccounts({ items: [{ id: "synthetic", request_inflight: value }] });
    assert.equal(account.requestInflight, expected);
  }
});
const grok = parseGrokAccounts({ items: [
  { id: "same-id", name: "Video studio", email: "grok@example.test", tier: "heavy", enabled: true, status: "active", quotas: { video: { remaining: 0 } } },
  { id: "disabled", name: "Paused", enabled: false, status: "reauth_required" },
] });

test("one pool preserves channel identity even when identifiers overlap", () => {
  const rows = poolRows(openai, grok, 0);
  assert.equal(rows.length, 4);
  assert.equal(new Set(rows.map(row => row.key)).size, 4);
  assert.deepEqual(rows.map(row => row.channel), ["chatgpt", "codex", "grok", "grok"]);
  assert.deepEqual(partitionPoolRows([rows[0], rows[2]]), { tokens: ["same-id"], grokIds: ["same-id"] });
  assert.deepEqual(partitionPoolRows([]), { tokens: [], grokIds: [] });
});

test("search, channel, tier and status filters compose across both pools", () => {
  const rows = poolRows(openai, grok, 0);
  assert.equal(filterPoolRows(rows, " GROK@ ", "grok", "heavy", "正常").length, 1);
  assert.equal(filterPoolRows(rows, "studio", "all", "all", "all").length, 1);
  assert.equal(filterPoolRows(rows, "codex", "codex", "Free", "异常").length, 1);
  assert.equal(filterPoolRows(rows, "", "grok", "heavy", "禁用").length, 0);
  assert.equal(filterPoolRows(rows, "absent", "all", "all", "all").length, 0);
  assert.deepEqual(partitionPoolRows(rows.slice(1, 3)), { tokens: ["codex-test"], grokIds: ["same-id"] });
});

test("log account links can search stable IDs without searching access tokens", () => {
  const rows = poolRows([{ ...openai[0], pool_account_id: "pool-stable-id" }], grok, 0);
  assert.equal(filterPoolRows(rows, "pool-stable-id", "all", "all", "all").length, 1);
  assert.equal(filterPoolRows(rows, "same-id", "all", "all", "all").length, 1);
});

test("Grok statuses respect disabled, reauthentication and cooldown boundaries", () => {
  const base = grok[0];
  assert.equal(grokStatus({ ...base, cooldownUntil: 20 }, 19999), "限流");
  assert.equal(grokStatus({ ...base, cooldownUntil: 20 }, 20000), "正常");
  assert.equal(grokStatus({ ...base, status: "reauth_required", cooldownUntil: 20 }, 0), "异常");
  assert.equal(grokStatus({ ...base, status: "unknown" }, 0), "异常");
  assert.equal(grokStatus({ ...base, enabled: false, status: "reauth_required" }, 0), "禁用");
});

test("invalid external rows are skipped and missing quotas stay unknown", () => {
  assert.deepEqual(parseGrokAccounts(null), []);
  assert.deepEqual(parseGrokAccounts({ items: [null, {}, { id: 4 }] }), []);
  const parsed = parseGrokAccounts({ items: [{ id: "valid", enabled: "false", quotas: { chat: { remaining: "oops" }, image: { remaining: 0 } } }] })[0];
  assert.equal(parsed.enabled, false);
  assert.equal(parsed.name, "Grok 账号");
  assert.equal(parsed.quotas.chat.remaining, null);
  assert.equal(parsed.quotas.image.remaining, 0);
});
