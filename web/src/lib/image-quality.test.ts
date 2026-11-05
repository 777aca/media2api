import assert from "node:assert/strict";
import test from "node:test";

import { getImageQualityOptions, normalizeImageQuality } from "./image-quality";

const values = (model: string) => getImageQualityOptions(model).map((option) => option.value);

test("网页图片型号支持扩展质量档位", () => {
  for (const model of ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2.6-aurora", "GPT-Image-2.6-Dawn"]) {
    assert.deepEqual(values(model), ["auto", "low", "medium", "high", "xhigh", "max"], model);
  }
});

test("其他型号只有标准档位，Grok 只有自动", () => {
  for (const model of ["gpt-image-2", "gpt-image-2.5", "gpt-image-2.6", "codex-gpt-image-2"]) {
    assert.deepEqual(values(model), ["auto", "low", "medium", "high"], model);
  }
  assert.deepEqual(values("grok-imagine-image"), ["auto"]);
});

test("不支持的质量档位回落到 auto", () => {
  assert.equal(normalizeImageQuality("gpt-image-2", "max"), "auto");
  assert.equal(normalizeImageQuality("gpt-image-2.6-aurora", "max"), "max");
  assert.equal(normalizeImageQuality("gpt-image-2.6-aurora", "bogus"), "auto");
});
