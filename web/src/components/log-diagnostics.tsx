"use client";

import Link from "next/link";
import { Copy, ExternalLink } from "lucide-react";
import { toast } from "sonner";
import type { ReactNode } from "react";
import type { SystemLog } from "@/lib/api";
import { assetSummaries, durationText, errorLabels, executionRecords, inputUploads, kindLabels, logChannelLabels, logKind, logLabel, logNumber, logRecord, logStatus, logText, modelPair, outputCount, phaseLabels, safeResultUrls } from "@/lib/log-diagnostics";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LogRequestParameters } from "@/components/log-request-parameters";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="min-w-0 space-y-1"><dt className="text-xs text-muted-foreground">{label}</dt><dd className="break-all text-sm">{children}</dd></div>;
}

function quotaTime(value: unknown) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return logText(value);
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return "未知";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).format(date);
}

function CopyValue({ value }: { value: unknown }) {
  const text = logText(value);
  return <span className="inline-flex max-w-full items-start gap-1"><span className="break-all font-mono text-xs">{text}</span>{text !== "未知" && <Button variant="ghost" size="icon" className="size-5 shrink-0" aria-label="复制标识" onClick={async () => { try { await navigator.clipboard.writeText(text); toast.success("已复制"); } catch { toast.error("复制失败，请手动复制"); } }}><Copy className="size-3" /></Button>}</span>;
}

export function LogAccountLink({ detail }: { detail: Record<string, unknown> }) {
  const account = logText(detail.account_email, "") || logText(detail.account_id, "");
  return account ? <Link href={`/accounts/?q=${encodeURIComponent(account)}`} className="inline-flex max-w-full items-center gap-1 text-foreground hover:underline" title={account}><span className="truncate">{account}</span><ExternalLink className="size-3 shrink-0 text-muted-foreground" /></Link> : <span className="text-muted-foreground">未知</span>;
}

export function LogDiagnostics({ item, onPreview }: { item: SystemLog; onPreview: (index: number) => void }) {
  const detail = logRecord(item.detail), models = modelPair(detail), status = logStatus(detail);
  const assets = assetSummaries(detail.request_params), executions = executionRecords(detail), quota = logRecord(detail.quota_snapshot), usage = logRecord(detail.usage);
  const urls = safeResultUrls(detail), kind = logKind(detail), uploads = inputUploads(detail);
  return <div className="space-y-4">
    <section aria-label="调用概览" className="rounded-xl border p-4">
      <div className="mb-4 flex flex-wrap items-center gap-2"><Badge variant={status.variant}>{status.label}</Badge><Badge variant="secondary">{kindLabels[kind]}</Badge><span className="text-xs text-muted-foreground">{item.time} · UTC+8</span></div>
      <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label="请求模型">{models.requested}</Field><Field label="实际模型">{models.actual}{detail.actual_model_source === "upstream_request" && <span className="ml-2 text-xs text-muted-foreground">上游请求值</span>}{detail.actual_model_source === "upstream_response" && <span className="ml-2 text-xs text-muted-foreground">上游返回值</span>}</Field>
        <Field label="账号"><LogAccountLink detail={detail} /></Field><Field label="渠道">{logLabel(logChannelLabels, detail.channel)}</Field>
        <Field label="请求 ID"><CopyValue value={detail.request_id} /></Field><Field label="任务 ID"><CopyValue value={detail.task_id ?? detail.image_task_id} /></Field>
        <Field label="接口">{logText(detail.endpoint)}</Field><Field label="会话 ID"><CopyValue value={detail.conversation_id} /></Field>
        <Field label="产出 / 请求数量">{outputCount(detail)}</Field><Field label="重试次数">{logText(detail.retry_count)}</Field>
        {!!detail.actual_tool_model && <Field label="图片工具模型">{logText(detail.actual_tool_model)}</Field>}
      </dl>
    </section>
    <section aria-label="耗时与用量" className="rounded-xl border p-4">
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Field label="总耗时">{durationText(detail.duration_ms)}</Field><Field label="排队耗时">{durationText(detail.queue_duration_ms)}</Field><Field label="执行耗时">{durationText(detail.execution_duration_ms)}</Field><Field label="首字耗时">{durationText(detail.first_token_ms)}</Field>
        <Field label="输入 Token">{logText(usage.input_tokens ?? usage.prompt_tokens)}</Field><Field label="输出 Token">{logText(usage.output_tokens ?? usage.completion_tokens)}</Field>
      </dl>
      <div className="mt-4 space-y-2 border-t pt-3 text-xs"><p className="font-medium">账号额度快照</p>{quota.scope === "pool_account_image" ? <><div className="flex flex-wrap gap-3">{Object.keys(logRecord(quota.dimensions)).length ? Object.entries(logRecord(quota.dimensions)).map(([dimension, value]) => <span key={dimension}>{logLabel({ image_pro: "图片生成", image_edit: "图片编辑" }, dimension)}：{logText(logRecord(value).remaining)} 次</span>) : <span>图片剩余：{logText(quota.remaining)} 次</span>}</div><p className="text-muted-foreground">同步于 {quotaTime(quota.updated_at)}（UTC+8），非即时额度</p></> : <p className="text-muted-foreground">未知，未记录有同步时间依据的账号额度。</p>}</div>
    </section>
    {!!(detail.error || detail.error_code || detail.error_category) && <section aria-label="错误信息" className="space-y-2 rounded-xl border border-destructive/30 bg-destructive/5 p-4">
      <h3 className="text-sm font-medium text-destructive">错误信息</h3>
      <p className="text-xs text-muted-foreground">{logLabel(errorLabels, detail.error_category)} · 阶段：{logLabel(phaseLabels, detail.phase)} · 上游状态：{logText(detail.upstream_status)}</p>
      <p className="break-all font-mono text-xs">{logText(detail.error_code)}</p><p className="whitespace-pre-wrap break-words text-sm">{logText(detail.error)}</p>
    </section>}
    <section aria-label="输入素材摘要" className="space-y-3 rounded-xl border p-4">
      <h3 className="text-sm font-medium">输入素材摘要{assets.length ? ` · ${assets.length} 个` : ""}</h3>
      <p className="text-xs text-muted-foreground">提交给上游的素材数：{logText(detail.submitted_input_count)}</p>
      {assets.length ? assets.map((asset, index) => <div key={asset.path} className="space-y-2 rounded-lg bg-secondary/40 p-3 text-xs">
        <p className="break-all text-muted-foreground">素材 {index + 1} · {asset.path}</p>
        <div className="flex flex-wrap gap-x-4 gap-y-2"><span>格式：{asset.mime_type || "未知"}</span><span>尺寸：{asset.width && asset.height ? `${asset.width} × ${asset.height}` : "未知"}</span><span>大小：{asset.size_bytes === undefined ? "未知" : `${(asset.size_bytes / 1024).toFixed(1)} KB`}</span></div>
        <p>解码：{logLabel({ valid: "有效", invalid: "无效", omitted_too_large: "超过摘要处理上限" }, asset.decode_status)}</p>
        <div className="flex items-start gap-2"><span className="shrink-0 text-muted-foreground">SHA-256</span><CopyValue value={asset.sha256} /></div>
      </div>) : <p className="text-xs text-muted-foreground">未记录素材摘要。没有素材或旧日志均可能出现此情况。</p>}
      <p className="text-xs text-muted-foreground">解码有效不代表上传成功；未取得上传确认时显示未知。</p>
      <div className="space-y-2 border-t pt-3"><h4 className="text-xs font-medium">上游上传记录</h4>{uploads.length ? uploads.map((upload, index) => <div key={String(upload.upload_id)} className="space-y-2 rounded-lg bg-secondary/40 p-3 text-xs">
        <p>第 {index + 1} 条 · {logLabel({ confirmed: "已确认上传", failed: "上传失败", started: "上传中" }, upload.status)} · {durationText(upload.duration_ms)}</p>
        <p className="text-muted-foreground">阶段：{logLabel({ validate: "校验素材", create_file: "登记文件", upload_bytes: "上传文件", register_file: "登记文件", confirm_upload: "确认上传" }, upload.phase)}{upload.error_code ? ` · ${logText(upload.error_code)}` : ""}</p>
        <div className="flex items-start gap-2"><span className="shrink-0 text-muted-foreground">上传指纹</span><CopyValue value={upload.sha256} /></div>
      </div>) : <p className="text-xs text-muted-foreground">未知，未记录上传确认。</p>}<p className="text-xs text-muted-foreground">上传指纹对应实际上传字节；素材经过压缩或格式转换后，可能与输入指纹不同。</p></div>
    </section>
    <section aria-label="执行过程" className="space-y-3 rounded-xl border p-4">
      <h3 className="text-sm font-medium">执行过程</h3>
      {executions.length ? executions.map((row, index) => <div key={index} className="space-y-2 rounded-lg bg-secondary/40 p-3 text-xs">
        <div className="flex flex-wrap items-center justify-between gap-2"><span>{logNumber(row.attempt) === undefined ? `子任务 ${index + 1}` : `第 ${row.attempt} 次尝试`}</span><span className="text-muted-foreground">{durationText(row.duration_ms)}</span></div>
        <div className="flex flex-wrap gap-3"><LogAccountLink detail={row} /><span>{logLabel(phaseLabels, row.phase)}</span><span>{logLabel({ success: "成功", error: "失败", failed: "失败", queued: "排队", running: "执行中", cancelled: "已取消", uncertain: "结果待确认" }, row.status ?? row.outcome)}</span>{logNumber(row.retry_count) !== undefined && <span>重试：{logText(row.retry_count)}</span>}</div>
        {!!row.actual_model && <p className="break-all text-muted-foreground">执行模型：{logText(row.actual_model)}{row.actual_tool_model ? ` · 图片工具：${logText(row.actual_tool_model)}` : ""}</p>}
        {!!(row.error_code || row.error_category) && <p className="break-all text-destructive">{logLabel(errorLabels, row.error_category)} · {logText(row.error_code)}</p>}
        {row.image_child_id ? <p className="break-all font-mono text-muted-foreground">{logText(row.image_child_id)}</p> : null}
        {Array.isArray(row.attempted_accounts) && <p className="break-all text-muted-foreground">已尝试账号（不代表顺序）：{row.attempted_accounts.filter(v => typeof v === "string").join("、") || "未知"}</p>}
      </div>) : <p className="text-xs text-muted-foreground">未记录执行明细，无法还原账号切换过程。</p>}
    </section>
    {!!urls.length && <section aria-label="生成结果" className="space-y-3 rounded-xl border p-4"><h3 className="text-sm font-medium">生成结果 · {urls.length} 个链接</h3><div className="flex flex-wrap gap-2">{urls.map((url, index) => kind === "image" ? <Button key={url} variant="outline" size="sm" onClick={() => onPreview(index)}>预览图片 {index + 1}</Button> : <Button key={url} variant="outline" size="sm" asChild><a href={url} target="_blank" rel="noreferrer">查看结果 {index + 1}<ExternalLink className="size-3" /></a></Button>)}</div></section>}
    <details className="rounded-xl border p-4"><summary className="cursor-pointer text-sm">请求参数</summary><div className="mt-3"><LogRequestParameters parameters={detail.request_params} /></div></details>
    <details className="rounded-xl border p-4"><summary className="cursor-pointer text-sm">完整日志 JSON</summary><pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-all text-xs leading-6 text-muted-foreground">{JSON.stringify(detail, null, 2)}</pre></details>
  </div>;
}
