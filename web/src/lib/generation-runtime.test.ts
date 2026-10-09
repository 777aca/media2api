import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { parseImageQuota, parseStatistics } from "./generation-runtime";

function statistics() {
  return {
    live: { running: 8, queued: 200, uncertain: 1, cooldown_accounts: 2, global_concurrency: 8, max_waiting_images: 200, keys: [{ key_id: "test-key", running: 4, queued: 2, uncertain: 0, concurrency_limit: 4 }] },
    summary: { requests: 0, successful_images: 0, partial_success: 0, failed: 0, cancelled: 0, uncertain: 0, platform_failures: 0, invalid_request: 0, content_rejected: 0, local_rejection: 0, platform_success_rate: null, queue_p50: null, queue_p95: null, generation_p50: null, generation_p95: null },
    filters: { models: ["gpt-image-2"], keys: ["test-key"] },
  };
}

describe("生图运行数据", () => {
  it("空统计保留 null，不能显示成 0% 成功率", () => {
    const parsed = parseStatistics(statistics());
    assert.equal(parsed.summary.platform_success_rate, null);
    assert.equal(parsed.summary.queue_p95, null);
    assert.equal(parsed.live.queued, 200);
    assert.equal(parsed.keys[0].values.running, 4);
  });
  it("拒绝损坏统计、错误计数和筛选字段", () => {
    assert.throws(() => parseStatistics(null));
    assert.throws(() => parseStatistics({ ...statistics(), summary: {} }));
    assert.throws(() => parseStatistics({ ...statistics(), live: { ...statistics().live, running: -1 } }));
    assert.throws(() => parseStatistics({ ...statistics(), summary: { ...statistics().summary, platform_success_rate: 90 } }));
    assert.throws(() => parseStatistics({ ...statistics(), filters: { models: [3], keys: [] } }));
  });
  it("明确区分不限额和剩余零张", () => {
    const unlimited = parseImageQuota({ image_quota_used: 7, image_quota_reserved: 2, image_quota_limit: null, image_quota_remaining: null });
    const exhausted = parseImageQuota({ image_quota_used: 7, image_quota_reserved: 2, image_quota_limit: 9, image_quota_remaining: 0 });
    assert.equal(unlimited.image_quota_remaining, null);
    assert.equal(exhausted.image_quota_remaining, 0);
    assert.throws(() => parseImageQuota({}));
    assert.throws(() => parseImageQuota({ image_quota_used: "7", image_quota_reserved: 0, image_quota_limit: null, image_quota_remaining: null }));
  });
});
