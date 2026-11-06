"use client";

import { ChevronRight, CircleAlert, Copy, LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import type { ModelCatalogState } from "@/hooks/use-model-catalog";
import { cn } from "@/lib/utils";

function formatSyncTime(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "尚未同步";
}

export function ModelCatalog({ state, kind = "image", compact = false }: { state: ModelCatalogState; kind?: "image" | "text"; compact?: boolean }) {
  const { catalog, error, loading, syncing, sync } = state;
  const models = catalog?.data ?? [];
  const hasGrok = models.some(model => model.source === "grok_web");
  const isText = kind === "text";
  const catalogLabel = isText ? "文本模型" : "图片";
  const syncState = catalog?.sync;
  const hasPreviousSync = models.length > 0 || Boolean(syncState?.last_success_at);
  const syncMessage = syncState?.status === "partial"
    ? `${catalogLabel}目录未完整更新，已保留最近成功的目录，请重新同步。`
    : syncState?.status === "failed"
      ? hasPreviousSync
        ? `${catalogLabel}目录同步失败，显示最近成功的目录。`
        : `尚未取得${catalogLabel}目录，请重新同步。`
      : null;
  const authFailed = isText && syncState?.errors.some(item => item.code === "upstream_http_401");

  const copyModel = async (id: string) => {
    try {
      await navigator.clipboard.writeText(id);
      toast.success("模型名已复制");
    } catch {
      toast.error("复制失败，请手动复制模型名");
    }
  };

  const content = (
    <div className="space-y-3">
      {!compact && <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium text-stone-700">
            {isText ? "可用文本模型" : hasGrok ? "可用模型" : "可用图片模型"} <span className="text-stone-400">({models.length})</span>
          </h3>
          <p className="mt-1 text-xs leading-5 text-stone-500">{isText ? "按已导入账号列出 ChatGPT、Codex 与 Grok 对话模型；点击复制原始模型 ID。" : hasGrok ? "当前号池可用的图片、Grok 对话与视频模型；点击模型名可复制。" : "展示本项目已接入、且当前账号池符合接入条件的图片型号；点击模型名可复制。"}</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="rounded-lg border-stone-200 bg-white text-stone-700"
          onClick={() => void sync()}
          disabled={loading}
        >
          <RefreshCw className={cn("size-3.5", loading && "animate-spin")} />
          {syncing ? "正在同步" : "同步目录"}
        </Button>
      </div>}
      <div className="space-y-1 text-xs leading-5 text-stone-500" aria-live="polite">
        <p>最近更新：{formatSyncTime(syncState?.last_success_at)}</p>
        {syncState?.status === "success" && !error && (
          <p>已根据 {syncState.source_count} 个活跃账号更新{isText ? "文本模型" : hasGrok ? "模型" : "图片"}目录。</p>
        )}
        {authFailed && <p className="text-amber-700">部分账号认证失败，请在账号管理更新登录凭据后同步目录。</p>}
        {(error || syncMessage) && (
          <div role="alert" className="flex items-start gap-1.5 text-amber-700">
            <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
            <span>{error ? `${error}${catalog ? "，已保留当前显示的目录。" : "。"}` : syncMessage}</span>
          </div>
        )}
      </div>
      <div className="flex flex-wrap gap-2" aria-busy={loading}>
        {models.map((model) => (
          <button
            key={model.id}
            type="button"
            className="inline-flex cursor-pointer flex-wrap items-center gap-1.5 rounded-lg border border-stone-200 bg-white px-2.5 py-1.5 text-xs font-medium text-stone-700 transition hover:border-stone-300 hover:bg-stone-50"
            onClick={() => void copyModel(model.id)}
            title={`点击复制 ${model.id}（${isText ? "文本对话模型，调用时保留原始 ID" : model.entry_kind === "web_image" ? "ChatGPT 网页会话生图，原样传递型号，账号权限由上游校验" : model.entry_kind === "image_api" ? "已接入图片接口，账号权限由上游校验" : "项目图片兼容入口"}）`}
          >
            <span className="break-all font-mono">{model.id}</span>
            {isText && model.display_name && model.display_name !== model.id && <span className="text-stone-500">{model.display_name}</span>}
            <span className="rounded bg-stone-100 px-1 py-0.5 text-[10px] text-stone-500">
              {isText ? (model.channels ?? (model.source === "grok_web" ? ["grok_web"] : [])).map(channel => ({ web: "ChatGPT", codex: "Codex", grok_web: "Grok" })[channel]).join(" / ") || "文本" : model.source === "grok_web" ? `Grok · ${model.capabilities?.join("/") || "Web"}` : model.entry_kind === "web_image" ? "网页生图" : model.entry_kind === "image_api" ? "图片接口" : "项目入口"}
            </span>
            <Copy className="size-3 text-stone-400" aria-hidden="true" />
          </button>
        ))}
        {models.length === 0 && loading && (
          <span className="flex items-center gap-1.5 text-sm text-stone-400"><LoaderCircle className="size-4 animate-spin" />正在加载{isText ? "文本" : "图片"}模型...</span>
        )}
        {models.length === 0 && !loading && !error && syncState?.status === "success" && (
          <span className="text-sm text-stone-400">当前暂无可用{isText ? "文本" : "图片"}模型，请先导入或启用账号。</span>
        )}
      </div>
      {!isText && models.some((model) => model.source === "compatibility") && (
        <p className="text-xs leading-5 text-stone-500">“项目入口”是图片兼容路由，“网页生图”通过 ChatGPT 网页会话原样请求所选型号。同步目录仅读取本地账号状态，实际账号权限和额度由上游校验。</p>
      )}
      {isText && <p className="text-xs leading-5 text-stone-500">ChatGPT / Codex 目录由对应账号同步并缓存，Grok 按导入账号等级列出已接入型号；实际调用受上游权限和额度限制。</p>}
    </div>
  );

  if (!compact) return content;

  const attention = Boolean(error || syncMessage || authFailed);
  const statusLabel = loading ? "更新中…" : authFailed ? "账号认证失败" : error ? "同步失败"
    : syncState?.status === "partial" ? "部分更新" : syncState?.status === "failed" ? "同步失败"
    : syncState?.last_success_at ? "已更新" : "尚未同步";
  const statusDetail = [error || syncMessage, authFailed ? "部分账号认证失败，请更新登录凭据" : "",
    `最近更新：${formatSyncTime(syncState?.last_success_at)}`].filter(Boolean).join("；");

  return <div className="relative">
    <details className="group/catalog">
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-lg py-2 pr-24 text-sm outline-none hover:bg-stone-50 focus-visible:ring-2 focus-visible:ring-blue-500 dark:hover:bg-stone-900 [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden="true" className="size-3.5 shrink-0 text-stone-400 transition-transform group-open/catalog:rotate-90 motion-reduce:transition-none" />
        <span className="shrink-0 font-medium text-stone-700">{isText ? "文本模型" : "图片模型"}</span>
        <span className="rounded bg-stone-100 px-1.5 py-0.5 text-xs tabular-nums text-stone-500">{models.length}</span>
        <span title={statusDetail} role={attention ? "status" : undefined} className={cn("ml-1 min-w-0 truncate text-xs", attention ? "text-amber-600" : "text-stone-400")}>
          {statusLabel}{attention && models.length > 0 ? " · 已保留可用模型" : ""}
        </span>
      </summary>
      <div className="max-h-72 overflow-y-auto border-t border-stone-100 px-1 py-3">{content}</div>
    </details>
  </div>;
}
