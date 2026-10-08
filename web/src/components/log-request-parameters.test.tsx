import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { LogRequestParameters } from "./log-request-parameters";

describe("日志请求参数", () => {
  it("展示具体参数、脱敏说明与复制按钮", () => {
    const html = renderToStaticMarkup(<LogRequestParameters parameters={{ prompt: "测试提示词", model: "gpt-image-2", n: 2, quality: "high" }} />);
    for (const text of ["请求参数", "测试提示词", "gpt-image-2", "high", "复制 JSON", "敏感字段已脱敏", "[TRUNCATED]"]) {
      assert.ok(html.includes(text));
    }
    assert.ok(!html.includes("此日志未记录"));
  });

  it("缺失或错误类型的历史数据明确提示无法补全", () => {
    for (const parameters of [undefined, null, "invalid", [], 1]) {
      const html = renderToStaticMarkup(<LogRequestParameters parameters={parameters} />);
      assert.match(html, /此日志未记录请求参数/);
      assert.doesNotMatch(html, /复制 JSON/);
    }
  });

  it("空对象是已记录的空请求，参数内容作为文本转义", () => {
    assert.match(renderToStaticMarkup(<LogRequestParameters parameters={{}} />), /复制 JSON/);
    const html = renderToStaticMarkup(<LogRequestParameters parameters={{ prompt: "<script>alert(1)</script>" }} />);
    assert.doesNotMatch(html, /<script>/);
    assert.match(html, /&lt;script&gt;/);
  });
});
