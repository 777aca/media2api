import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createImageTaskBatch, parseImageQuota } from "./generation-runtime";

describe("生图运行数据", () => {
  it("拒绝批量任务，不拆分成多个请求", async () => {
    for (const ids of [[], ["one", "two"]]) {
      await assert.rejects(createImageTaskBatch(ids, [], "draw", "gpt-image-2", "auto", "auto"), /仅支持生成 1 张图片/);
    }
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
