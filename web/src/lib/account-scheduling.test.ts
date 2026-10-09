import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseAccountScheduling } from "./account-scheduling";

describe("账号调度表单", () => {
  it("接受默认值、边界值和整数文本", () => {
    assert.deepEqual(parseAccountScheduling("0", "1"), { priority: 0, weight: 1 });
    assert.deepEqual(parseAccountScheduling(1000, 1000), { priority: 1000, weight: 1000 });
    assert.deepEqual(parseAccountScheduling(" 10 ", "3"), { priority: 10, weight: 3 });
  });

  it("拒绝空值、布尔值、小数及其他非整数输入", () => {
    for (const value of ["", " ", null, undefined, true, false, [], {}, 1.5, "1.5", "NaN", Infinity, "1e3"]) {
      assert.throws(() => parseAccountScheduling(value, 1), /优先级/);
      assert.throws(() => parseAccountScheduling(0, value), /权重/);
    }
  });

  it("拒绝越界值，权重不能为零", () => {
    for (const value of [-1, 1001, "-1", "1001"]) {
      assert.throws(() => parseAccountScheduling(value, 1), /优先级/);
      assert.throws(() => parseAccountScheduling(0, value), /权重/);
    }
    assert.throws(() => parseAccountScheduling(0, 0), /权重/);
  });
});
