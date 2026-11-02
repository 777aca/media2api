import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { imageRecoveryMessage, isAutomaticImageRecovery } from "./image-recovery";
import { parseTasks } from "./generation-runtime";
import type { ImageTask } from "./api";

function task(values: Partial<ImageTask> = {}): ImageTask {
  return { id: "synthetic", task_id: "synthetic", status: "uncertain", mode: "generate", model: "gpt-image-2",
    created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:00Z", recovery_status: "auto_pending",
    recovery_attempts: 1, recovery_max_attempts: 3, recovery_next_at: 130, recovery_deadline: 700, ...values };
}

describe("自动结果恢复", () => {
  it("等待和执行任务保持自动恢复状态", () => {
    assert.equal(isAutomaticImageRecovery(task()), true);
    assert.match(imageRecoveryMessage(task(), 100_000), /30 秒后再次读取.*1\/3/);
    assert.match(imageRecoveryMessage(task({ status: "running", recovery_status: "recovering_result" }), 100_000), /正在自动读取/);
    assert.equal(isAutomaticImageRecovery(task({ status: "queued", recovery_status: "", recovery_active: true })), true);
    assert.equal(isAutomaticImageRecovery(task({ status: "success", recovery_status: "completed" })), false);
  });
  it("已结束的任务不再显示恢复中的提示", () => {
    assert.equal(imageRecoveryMessage(task({ status: "success", recovery_status: "completed" })), "");
    assert.equal(imageRecoveryMessage(task({ status: "error", recovery_status: "auto_failed" })), "");
  });
  it("恢复字段验证拒绝错误类型，兼容没有新字段的旧响应", () => {
    assert.equal(parseTasks({ items: [task()] }).length, 1);
    for (const value of [{ recovery_attempts: null }, { recovery_attempts: -1 }, { recovery_attempts: 0.5 }, { recovery_deadline: "soon" }, { recovery_active: "true" }]) {
      assert.throws(() => parseTasks({ items: [{ ...task(), ...value }] }));
    }
    assert.equal(parseTasks({ items: [{ id: "old", status: "error", mode: "generate", created_at: "old", updated_at: "old" }] }).length, 1);
  });
});
