"use client";

import { CircleAlert, Copy, LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import type { ModelCatalogState } from "@/hooks/use-model-catalog";
import { cn } from "@/lib/utils";

function formatSyncTime(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "尚未刷新";
}

export function ModelCatalog({ state }: { state: ModelCatalogState }) {
  const { catalog, error, loading, syncing, sync } = state;
  const models = catalog?.data ?? [];
  const syncState = catalog?.sync;
  const hasPreviousSync = models.length > 0 || Boolean(syncState?.last_success_at);
  const syncMessage = syncState?.status === "partial"
    ? "图片目录未完整更新，已保留最近成功的目录，请重新刷新。"
    : syncState?.status === "failed"
      ? hasPreviousSync
        ? "图片目录刷新失败，显示最近成功的目录。"
        : "尚未取得图片目录，请重新刷新。"
      : null;

  const copyModel = async (id: string) => {
    try {
      await navigator.clipboard.writeText(id);
      toast.success("模型名已复制");
    } catch {
      toast.error("复制失败，请手动复制模型名");
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium text-stone-700">
            可用图片模型 <span className="text-stone-400">({models.length})</span>
          </h3>
          <p className="mt-1 text-xs leading-5 text-stone-500">展示本项目已接入、且当前账号池符合接入条件的图片型号；点击模型名可复制。</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="rounded-lg border-stone-200 bg-white text-stone-700"
          onClick={() => void sync()}
          disabled={loading}
        >
          <RefreshCw className={cn("size-3.5", loading && "animate-spin")} />
          {syncing ? "正在刷新" : "刷新目录"}
        </Button>
      </div>
      <div className="space-y-1 text-xs leading-5 text-stone-500" aria-live="polite">
        <p>最近更新：{formatSyncTime(syncState?.last_success_at)}</p>
        {syncState?.status === "success" && !error && (
          <p>已根据 {syncState.source_count} 个活跃账号更新图片目录。</p>
        )}
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
            title={`点击复制 ${model.id}（${model.entry_kind === "web_image" ? "ChatGPT 网页会话生图，原样传递型号，账号权限由上游校验" : model.entry_kind === "image_api" ? "已接入图片接口，账号权限由上游校验" : "项目图片兼容入口"}）`}
          >
            <span className="break-all font-mono">{model.id}</span>
            <span className="rounded bg-stone-100 px-1 py-0.5 text-[10px] text-stone-500">
              {model.entry_kind === "web_image" ? "网页生图" : model.entry_kind === "image_api" ? "图片接口" : "项目入口"}
            </span>
            <Copy className="size-3 text-stone-400" aria-hidden="true" />
          </button>
        ))}
        {models.length === 0 && loading && (
          <span className="flex items-center gap-1.5 text-sm text-stone-400"><LoaderCircle className="size-4 animate-spin" />正在加载图片模型...</span>
        )}
        {models.length === 0 && !loading && !error && syncState?.status === "success" && (
          <span className="text-sm text-stone-400">当前暂无可用图片模型，请先导入或启用账号。</span>
        )}
      </div>
      {models.some((model) => model.source === "compatibility") && (
        <p className="text-xs leading-5 text-stone-500">“项目入口”是图片兼容路由，“网页生图”通过 ChatGPT 网页会话原样请求所选型号。刷新目录仅读取本地账号状态，实际账号权限和额度由上游校验。</p>
      )}
    </div>
  );
}
