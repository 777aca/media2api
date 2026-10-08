import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { formatImageRetention, IMAGE_RETENTION_MAX_HOURS, parseImageRetentionHours, parseImageRetentionResponse } from "./image-retention";

describe("图片有效期", () => {
  it("接受最低 1 小时并保留已有按天配置的时长", () => {
    assert.equal(parseImageRetentionResponse({ config: { image_retention_days: 15 } }), 360);
    assert.equal(parseImageRetentionResponse({ config: { image_retention_hours: 1, image_retention_days: 15 } }), 1);
    assert.equal(parseImageRetentionHours(" 24 "), 24);
    assert.equal(parseImageRetentionHours(1), 1);
    assert.equal(parseImageRetentionHours(IMAGE_RETENTION_MAX_HOURS), IMAGE_RETENTION_MAX_HOURS);
  });

  it("拒绝不足 1 小时、空值、小数、布尔值及过大的时长", () => {
    for (const value of ["", " ", "0.5", "1.5", "1e2", 0, -1, 0.5, 1.5, true, null, undefined, Infinity, NaN, IMAGE_RETENTION_MAX_HOURS + 1, {}]) {
      assert.throws(() => parseImageRetentionHours(value), /整数（小时）/);
    }
  });

  it("小时配置无效时不能退回旧天数掩盖错误", () => {
    assert.throws(() => parseImageRetentionResponse({ config: { image_retention_hours: 0, image_retention_days: 15 } }));
    assert.throws(() => parseImageRetentionResponse({ config: { image_retention_days: 36501 } }));
  });

  it("展示明确的小时单位，并为整天数补充换算", () => {
    assert.equal(formatImageRetention(1), "1 小时");
    assert.equal(formatImageRetention(25), "25 小时");
    assert.equal(formatImageRetention(360), "360 小时（15 天）");
  });

  it("加载失败或配置缺失时不能假定已启用默认值", () => {
    for (const value of [null, {}, { config: null }, { config: {} }, { config: { image_retention_days: "invalid" } }]) {
      assert.throws(() => parseImageRetentionResponse(value));
    }
  });
});
