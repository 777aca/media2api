"use client";

import { Copy } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";

export function LogRequestParameters({ parameters }: { parameters: unknown }) {
  const hasParameters = parameters !== null && typeof parameters === "object" && !Array.isArray(parameters);
  const formatted = hasParameters ? JSON.stringify(parameters, null, 2) : "";

  async function copyParameters() {
    try {
      await navigator.clipboard.writeText(formatted);
      toast.success("请求参数已复制");
    } catch {
      toast.error("复制失败，请手动选择参数复制");
    }
  }

  return (
    <section className="space-y-3 rounded-xl border border-stone-200 p-4" aria-label="请求参数">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-medium text-stone-700">请求参数</h3>
        {hasParameters ? (
          <Button variant="outline" size="sm" className="rounded-lg" onClick={() => void copyParameters()}>
            <Copy className="size-3.5" />复制 JSON
          </Button>
        ) : null}
      </div>
      {hasParameters ? (
        <>
          <p className="text-xs leading-5 text-stone-400">
            已记录传入参数；敏感字段已脱敏，文件与 Base64 内容已省略。超长内容标记为 [TRUNCATED]。
          </p>
          <pre className="max-h-[48vh] overflow-auto whitespace-pre-wrap break-all rounded-lg bg-stone-50 p-3 text-xs leading-6 text-stone-700">
            {formatted}
          </pre>
        </>
      ) : (
        <p className="text-sm text-stone-400">此日志未记录请求参数。更新后的新调用会自动记录，历史记录无法补全。</p>
      )}
    </section>
  );
}
