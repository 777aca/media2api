import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseModelCatalogResponse } from "./model-catalog";

function model(id: string, source = "official") {
  return { id, object: "model", created: 0, owned_by: "openai", permission: [], root: id, parent: null, source };
}

function catalog(data: unknown[] = [model("official-test-model")]) {
  return {
    object: "list",
    data,
    sync: {
      status: "success",
      last_attempt_at: "2026-10-08T12:00:00+00:00",
      last_success_at: "2026-10-08T12:00:00+00:00",
      source_count: 2,
      successful_sources: 2,
      errors: [],
    },
  };
}

describe("模型目录响应契约", () => {
  it("保留官方 ID，区分官方模型和项目入口", () => {
    const parsed = parseModelCatalogResponse(catalog([model("official-test-model"), model("gpt-image-2", "compatibility")]));
    assert.deepEqual(parsed.data.map(({ id, source }) => ({ id, source })), [
      { id: "official-test-model", source: "official" },
      { id: "gpt-image-2", source: "compatibility" },
    ]);
    assert.equal(parsed.sync.successful_sources, 2);
  });

  it("重复模型只展示一次，官方记录优先", () => {
    const parsed = parseModelCatalogResponse(catalog([
      model("same-model", "compatibility"), model("same-model"), model("same-model", "compatibility"),
    ]));
    assert.equal(parsed.data.length, 1);
    assert.equal(parsed.data[0].source, "official");
  });

  it("首次同步失败允许空目录和 null 同步时间", () => {
    const parsed = parseModelCatalogResponse({
      ...catalog([]),
      sync: {
        status: "failed", last_attempt_at: "2026-10-08T12:00:00Z", last_success_at: null,
        source_count: 1, successful_sources: 0,
        errors: [{ account_id: null, source: "anonymous", code: "upstream_http_403" }],
      },
    });
    assert.equal(parsed.sync.status, "failed");
    assert.equal(parsed.sync.last_success_at, null);
  });

  it("接受部分失败及安全错误代码", () => {
    const parsed = parseModelCatalogResponse({
      ...catalog(),
      sync: { ...catalog().sync, status: "partial", successful_sources: 1, errors: [
        { account_id: "stable-internal-id", source: "account", code: "invalid_response" },
      ] },
    });
    assert.equal(parsed.sync.errors[0].code, "invalid_response");
  });

  it("拒绝非对象或非列表响应", () => {
    for (const value of [null, [], "invalid", { ...catalog(), object: "models" }, { ...catalog(), data: {} }]) {
      assert.throws(() => parseModelCatalogResponse(value), /响应格式不正确/);
    }
  });

  it("拒绝未声明的模型来源和同步状态", () => {
    assert.throws(() => parseModelCatalogResponse(catalog([model("test", "unknown-source")])), /响应格式不正确/);
    assert.throws(() => parseModelCatalogResponse({ ...catalog(), sync: { ...catalog().sync, status: "running" } }), /响应格式不正确/);
  });

  it("拒绝不完整或错误类型的模型字段", () => {
    for (const badModel of [
      { ...model("test"), id: " " }, { ...model("test"), created: "123" },
      { ...model("test"), created: Infinity }, { ...model("test"), parent: {} },
      { ...model("test"), permission: {} }, { ...model("test"), root: null },
    ]) {
      assert.throws(() => parseModelCatalogResponse(catalog([badModel])), /响应格式不正确/);
    }
  });

  it("拒绝非法来源计数和同步时间", () => {
    for (const badSync of [
      { ...catalog().sync, source_count: -1 }, { ...catalog().sync, source_count: 1.5 },
      { ...catalog().sync, successful_sources: 3 }, { ...catalog().sync, last_success_at: "not-a-date" },
      { ...catalog().sync, last_attempt_at: undefined },
    ]) {
      assert.throws(() => parseModelCatalogResponse({ ...catalog(), sync: badSync }), /响应格式不正确/);
    }
  });

  it("错误代码不允许凭据、错误正文或控制字符", () => {
    for (const code of ["Bearer fake-token", "someone@example.test", "ey.fake.jwt", "invalid_response\ncredential", "a".repeat(65)]) {
      const badValue = {
        ...catalog(), sync: { ...catalog().sync, errors: [{ account_id: null, source: "anonymous", code }] },
      };
      assert.throws(() => parseModelCatalogResponse(badValue), (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message.includes(code), false);
        return true;
      });
    }
  });

  it("保留 Codex 官方名称并合并同 ID 的两种通道", () => {
    const parsed = parseModelCatalogResponse(catalog([
      { ...model("same-official"), channels: ["web"] },
      { ...model("same-official"), channels: ["codex"], display_name: "Official Name" },
    ]));
    assert.equal(parsed.data.length, 1);
    assert.deepEqual(parsed.data[0].channels, ["web", "codex"]);
    assert.equal(parsed.data[0].display_name, "Official Name");
  });

  it("独立图片接口与官方目录条目区分，拒绝未知类型", () => {
    const parsed = parseModelCatalogResponse(catalog([
      { ...model("gpt-image-2.5-flare", "compatibility"), entry_kind: "image_api" },
      { ...model("gpt-image-2.5-sunburst", "compatibility"), entry_kind: "web_image" },
    ]));
    assert.equal(parsed.data[0].entry_kind, "image_api");
    assert.equal(parsed.data[1].entry_kind, "web_image");
    assert.equal(parsed.data[0].source, "compatibility");
    assert.throws(() => parseModelCatalogResponse(catalog([
      { ...model("image-test", "compatibility"), entry_kind: "untrusted" },
    ])), /响应格式不正确/);
  });

  it("校验模型和失败来源通道，拒绝未知通道", () => {
    const parsed = parseModelCatalogResponse({ ...catalog(), sync: { ...catalog().sync, status: "partial", errors: [
      { account_id: "internal-id", source: "account", channel: "codex", code: "upstream_http_403" },
    ] } });
    assert.equal(parsed.sync.errors[0].channel, "codex");
    for (const channels of [["untrusted"], "codex", [42]]) {
      assert.throws(() => parseModelCatalogResponse(catalog([{ ...model("test"), channels }])), /响应格式不正确/);
    }
    assert.throws(() => parseModelCatalogResponse({ ...catalog(), sync: { ...catalog().sync, errors: [
      { account_id: null, source: "anonymous", channel: "untrusted", code: "invalid_response" },
    ] } }), /响应格式不正确/);
  });
});
