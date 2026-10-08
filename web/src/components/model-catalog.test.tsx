import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import type { ModelCatalogState } from "../hooks/use-model-catalog";
import type { CatalogModel, ModelCatalogResponse } from "../lib/model-catalog";
import { ModelCatalog } from "./model-catalog";

function model(id: string, entryKind?: "web_image"): CatalogModel {
  return { id, source: "compatibility", object: "model", created: 0, owned_by: "media2api",
    permission: [], root: id, parent: null, ...(entryKind ? { entry_kind: entryKind } : {}) };
}

function catalog(overrides: Partial<ModelCatalogResponse> = {}): ModelCatalogResponse {
  return {
    object: "list",
    data: [model("gpt-image-2"), model("gpt-image-2.5-flare", "web_image"), model("gpt-image-2.5-sunburst", "web_image")],
    sync: {
      status: "success",
      last_attempt_at: "2026-10-08T12:00:00Z",
      last_success_at: "2026-10-08T12:00:00Z",
      source_count: 2,
      successful_sources: 2,
      errors: [],
    },
    ...overrides,
  };
}

function state(overrides: Partial<ModelCatalogState> = {}): ModelCatalogState {
  return {
    catalog: catalog(), error: null, loading: false, syncing: false,
    reload: async () => {}, sync: async () => {}, ...overrides,
  };
}

function renderText(value: ModelCatalogState) {
  return renderToStaticMarkup(<ModelCatalog state={value} />).replace(/<[^>]*>/g, "");
}

describe("本地图片模型目录", () => {
  it("展示网页生图和兼容入口，不宣称完成官方模型同步", () => {
    const text = renderText(state());
    assert.match(text, /可用图片模型 \(3\)/);
    assert.match(text, /gpt-image-2项目入口/);
    assert.match(text, /gpt-image-2.5-flare网页生图/);
    assert.match(text, /gpt-image-2.5-sunburst网页生图/);
    assert.match(text, /刷新目录仅读取本地账号状态/);
    assert.match(text, /实际账号权限和额度由上游校验/);
    assert.doesNotMatch(text, /官方模型|Codex 目录|同步成功/);
  });

  it("更新结果显示活跃账号数量和本地刷新时间", () => {
    const text = renderText(state());
    assert.match(text, /已根据 2 个活跃账号更新图片目录/);
    assert.match(text, /最近更新：/);
    assert.doesNotMatch(text, /尚未刷新|个来源/);
  });

  it("没有活跃账号时提示导入或启用账号", () => {
    const text = renderText(state({ catalog: catalog({ data: [], sync: {
      ...catalog().sync, source_count: 0, successful_sources: 0,
    } }) }));
    assert.match(text, /当前暂无可用图片模型，请先导入或启用账号/);
    assert.doesNotMatch(text, /刷新失败/);
  });

  it("首次请求失败显示原因，不误报为账号为空", () => {
    const text = renderText(state({ catalog: null, error: "图片目录服务暂时无法连接" }));
    assert.match(text, /图片目录服务暂时无法连接/);
    assert.match(text, /最近更新：尚未刷新/);
    assert.doesNotMatch(text, /导入或启用账号|已根据/);
  });

  it("部分更新失败时保留目录，且不展示内部账号标识", () => {
    const text = renderText(state({ catalog: catalog({ sync: {
      ...catalog().sync, status: "partial", successful_sources: 1,
      errors: [{ account_id: "synthetic-internal-id", source: "account", code: "upstream_timeout" }],
    } }) }));
    assert.match(text, /图片目录未完整更新，已保留最近成功的目录/);
    assert.match(text, /gpt-image-2.5-flare/);
    assert.doesNotMatch(text, /synthetic-internal-id/);
  });

  it("目录状态失败时说明保留已有结果", () => {
    const text = renderText(state({ catalog: catalog({ sync: {
      ...catalog().sync, status: "failed", last_success_at: null, successful_sources: 0,
    } }) }));
    assert.match(text, /图片目录刷新失败，显示最近成功的目录/);
    assert.match(text, /gpt-image-2.5-flare/);
  });

  it("HTTP 请求失败时保留模型并隐藏成功提示", () => {
    const text = renderText(state({ error: "图片目录服务暂时无法连接" }));
    assert.match(text, /图片目录服务暂时无法连接，已保留当前显示的目录/);
    assert.match(text, /gpt-image-2.5-flare/);
    assert.doesNotMatch(text, /已根据/);
  });

  it("首次加载和手动刷新期间禁用刷新按钮", () => {
    for (const syncing of [false, true]) {
      const html = renderToStaticMarkup(<ModelCatalog state={state({ catalog: null, loading: true, syncing })} />);
      const label = syncing ? "正在刷新" : "刷新目录";
      const refreshButton = [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
        .find((match) => match[2].includes(label));
      assert.ok(refreshButton, "应显示" + label + "按钮");
      assert.match(refreshButton[1], /\bdisabled(?:=""|\s|$)/);
      assert.match(html, /正在加载图片模型/);
    }
  });
});
