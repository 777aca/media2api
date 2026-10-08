import assert from "node:assert/strict";
import { test } from "node:test";
import { isUpdateRunning, parseSystemUpdate } from "./system-update";

const status = {
  current_version: "0.1.0", latest: null, has_update: false, checked_at: null,
  error: null, supported: false, reason: "源码运行", deployment: "source",
  job: null, rollback_version: null,
};
test("source mode and no published release remain explicit", () => {
  const value = parseSystemUpdate(status);
  assert.equal(value.latest, null);
  assert.equal(value.supported, false);
  assert.equal(isUpdateRunning(value.job), false);
});
test("malformed external update state is rejected", () => {
  for (const invalid of [null, [], {}, { ...status, supported: "false" }, { ...status, latest: {} }, { ...status, checked_at: "today" }]) {
    assert.throws(() => parseSystemUpdate(invalid));
  }
});
test("active jobs and terminal recovery states are distinguished", () => {
  for (const state of ["queued", "pulling", "restarting", "checking", "recovering", "succeeded", "rolled_back", "failed"]) {
    const value = parseSystemUpdate({ ...status, job: { id: "fake", operation: "update", version: "0.2.0", state, message: "测试", updated_at: 1 } });
    assert.equal(isUpdateRunning(value.job), !["succeeded", "rolled_back", "failed"].includes(state));
  }
});
