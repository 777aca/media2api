import assert from "node:assert/strict";
import { test } from "node:test";
import { assetSummaries, executionRecords, logKind, logLabel, logStatus, modelPair, outputCount, parseLogResponse, safeResultUrls } from "./log-diagnostics";

test("legacy logs do not invent an actual model or output count", () => {
  const detail = { model: "auto", endpoint: "/v1/chat/completions", urls: ["https://example.test/source"] };
  assert.deepEqual(modelPair(detail), { requested: "auto", actual: "未知" });
  assert.equal(outputCount(detail), "未知");
  assert.equal(logKind(detail), "chat");
});

test("known zero output is distinct from unknown and partial success", () => {
  assert.equal(outputCount({ requested_count: 2, output_count: 0 }), "0 / 2");
  assert.equal(logStatus({ status: "failed", requested_count: 2, output_count: 1 }).label, "部分成功");
  assert.equal(logStatus({}).label, "未知");
  assert.equal(logStatus({ status: "failed", partial_success: true }).label, "部分成功");
});

test("nested asset summaries keep safe metadata without returning binary content", () => {
  const assets = assetSummaries({ messages: [{ content: [{ image_url: { url: { type: "base64", content_omitted: true, sha256: "a".repeat(64), width: 640, height: 480, size_bytes: 123, decode_status: "valid" } } }] }], base64_images: ["unredacted legacy content"] });
  assert.equal(assets.length, 1);
  assert.equal(assets[0].width, 640);
  assert.equal(assets[0].path, "messages[0].content[0].image_url.url");
  assert.equal(assets[0].upload_status, undefined);
  assert.doesNotMatch(JSON.stringify(assets), /unredacted/);
});

test("result links reject script and protocol-relative URLs", () => {
  assert.deepEqual(safeResultUrls({ urls: ["javascript:alert(1)", "//external.test/file", "/files/result.psd", "https://example.test/result.png", null] }), ["/files/result.psd", "https://example.test/result.png"]);
});

test("empty attempt list still shows known child diagnostics", () => {
  assert.deepEqual(executionRecords({ attempts: [], image_tasks: [{ image_child_id: "child" }] }), [{ image_child_id: "child" }]);
  assert.deepEqual(executionRecords({ image_tasks: [{ image_child_id: "child", attempts: [{ attempt: 1, account_id: "a" }, { attempt: 2, account_id: "b" }] }] }), [
    { image_child_id: "child", attempt: 1, account_id: "a" }, { image_child_id: "child", attempt: 2, account_id: "b" },
  ]);
});

test("prototype keys are not accepted as task labels", () => {
  assert.equal(logKind({ task_type: "__proto__" }), "other");
  assert.equal(logKind({ task_type: "constructor" }), "other");
  assert.equal(logLabel({ web: "网页" }, "__proto__"), "__proto__");
  assert.equal(typeof logLabel({ web: "网页" }, "constructor"), "string");
});

test("malformed summary cannot become a React object child", () => {
  const result = parseLogResponse({items:[{id:"one",time:"now",type:"call",summary:{message:"bad"},detail:[]},null,{id:2}],total:1});
  assert.equal(result.items.length, 1);
  assert.equal(result.items[0].summary, undefined);
  assert.deepEqual(result.items[0].detail, {});
  assert.throws(() => parseLogResponse({items:"bad"}), /格式无效/);
});
