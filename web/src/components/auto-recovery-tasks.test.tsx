import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { AutoRecoveryTasks } from "./auto-recovery-tasks";
import { hasImageRecovery, imageRecoveryMessage, isAutomaticImageRecovery } from "../lib/image-recovery";
import { parseTasks } from "../lib/generation-runtime";
import type { ImageTask } from "../lib/api";

function task(values: Partial<ImageTask> = {}): ImageTask {
  return { id: "synthetic", task_id: "synthetic", status: "uncertain", mode: "generate", model: "gpt-image-2",
    created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:00Z", recovery_status: "auto_pending",
    recovery_attempts: 1, recovery_max_attempts: 3, recovery_next_at: 130, recovery_deadline: 700, ...values };
}

describe("自动结果恢复", () => {
  it("等待、执行和部分成功任务保持自动恢复状态", () => {
    const waiting = task();
    assert.equal(isAutomaticImageRecovery(waiting), true);
    assert.match(imageRecoveryMessage(waiting, 100_000), /30 秒后再次读取.*1\/3/);
    assert.match(imageRecoveryMessage(task({ status: "running", recovery_status: "recovering_result" }), 100_000), /正在自动读取/);
    assert.equal(isAutomaticImageRecovery(task({ status: "queued", recovery_status: "", recovery_active: true })), true);
    assert.equal(isAutomaticImageRecovery(task({ status: "success", recovery_status: "completed" })), false);
  });
  it("不再显示手动确认和结束任务按钮", () => {
    const html = renderToStaticMarkup(<AutoRecoveryTasks tasks={[task()]} now={100_000} />);
    assert.match(html, /自动结果恢复/);
    assert.match(html, /无需手动确认/);
    assert.match(html, /剩余恢复时间/);
    assert.doesNotMatch(html, /<button/);
    assert.doesNotMatch(html, /确认结束|结果待确认/);
  });
  it("成功结果可查看，失败原因及释放额度说明保留", () => {
    const success = task({ status: "success", recovery_status: "completed", data: [{ url: "/images/recovered.png" }] });
    const failed = task({ id: "failed", task_id: "failed", status: "error", recovery_status: "auto_failed", error: "原会话不可读取" });
    assert.equal(hasImageRecovery(success), true);
    assert.equal(hasImageRecovery(failed), true);
    const html = renderToStaticMarkup(<AutoRecoveryTasks tasks={[success, failed]} now={100_000} />);
    assert.match(html, /结果已恢复/);
    assert.match(html, /href="\/images\/recovered.png"/);
    assert.match(html, /预占额度已释放/);
    assert.match(html, /原会话不可读取/);
  });
  it("已完成记录不挤掉正在处理的任务", () => {
    const completed = Array.from({ length: 21 }, (_, index) => task({ id: `done-${index}`, task_id: `done-${index}`, status: "success", recovery_status: "completed" }));
    const html = renderToStaticMarkup(<AutoRecoveryTasks tasks={[...completed, task({ id: "pending", task_id: "pending" })]} now={100_000} />);
    assert.ok(html.indexOf("pending") < html.indexOf("done-0"));
    assert.doesNotMatch(html, /done-20/);
  });
  it("恢复字段验证拒绝错误类型，兼容没有新字段的旧响应", () => {
    assert.equal(parseTasks({ items: [task()] }).length, 1);
    for (const value of [{ recovery_attempts: null }, { recovery_attempts: -1 }, { recovery_attempts: 0.5 }, { recovery_deadline: "soon" }, { recovery_active: "true" }]) {
      assert.throws(() => parseTasks({ items: [{ ...task(), ...value }] }));
    }
    assert.equal(parseTasks({ items: [{ id: "old", status: "error", mode: "generate", created_at: "old", updated_at: "old" }] }).length, 1);
  });
});
