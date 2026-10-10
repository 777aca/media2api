import { httpRequest } from "./request";
import type { ImageTask } from "./api";

export type QueueSettings = {
  global_concurrency: number;
  key_concurrency: number;
  max_waiting_images: number;
  queue_timeout_seconds: number;
  task_retention_days: number;
};

export const DEFAULT_QUEUE: QueueSettings = { global_concurrency: 8, key_concurrency: 4, max_waiting_images: 200, queue_timeout_seconds: 600, task_retention_days: 30 };

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("运行数据格式不正确");
  return Object.fromEntries(Object.entries(value));
}

function numberRecord(value: unknown): Record<string, number | null> {
  const result: Record<string, number | null> = {};
  for (const [key, entry] of Object.entries(record(value))) {
    if (entry === null || (typeof entry === "number" && Number.isFinite(entry))) result[key] = entry;
  }
  return result;
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every((item): item is string => typeof item === "string")) throw new Error("统计筛选项格式不正确");
  return value;
}

function requireNumbers(source: Record<string, unknown>, keys: string[], nullable: string[] = []) {
  for (const key of keys) {
    const value = source[key];
    if (value === null && nullable.includes(key)) continue;
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new Error(`运行数据 ${key} 无效`);
  }
}

export function parseStatistics(value: unknown) {
  const source = record(value);
  const live = record(source.live);
  const filters = record(source.filters);
  const summary = record(source.summary);
  if (live.recovering === undefined) live.recovering = live.uncertain;
  if (summary.recovering === undefined) summary.recovering = summary.uncertain;
  requireNumbers(live, ["recovering"]);
  requireNumbers(summary, ["recovering"]);
  requireNumbers(live, ["running", "queued", "uncertain", "cooldown_accounts", "global_concurrency", "max_waiting_images"]);
  const nullable = ["platform_success_rate", "queue_p50", "queue_p95", "generation_p50", "generation_p95"];
  requireNumbers(summary, ["requests", "successful_images", "partial_success", "failed", "cancelled", "uncertain", "platform_failures", "invalid_request", "content_rejected", "local_rejection", ...nullable], nullable);
  if (typeof summary.platform_success_rate === "number" && summary.platform_success_rate > 1) throw new Error("平台成功率无效");
  const keys = Array.isArray(live.keys) ? live.keys.map((value) => {
    const item = record(value);
    if (item.recovering === undefined) item.recovering = item.uncertain;
    if (typeof item.key_id !== "string") throw new Error("Key 占用格式不正确");
    requireNumbers(item, ["running", "queued", "uncertain", "recovering", "concurrency_limit"]);
    return { key_id: item.key_id, values: numberRecord(item) };
  }) : [];
  return { summary: numberRecord(summary), live: numberRecord(live), keys, models: strings(filters.models), keyIds: strings(filters.keys) };
}

export async function fetchRuntimeStatistics(filters: { days: string; model: string; channel: string; key_id: string }) {
  return parseStatistics(await httpRequest<unknown>(`/api/runtime/statistics?${new URLSearchParams(filters)}`));
}

export async function fetchImageQuota() {
  return parseImageQuota(await httpRequest<unknown>("/api/image-quota"));
}

export function parseImageQuota(value: unknown) {
  const source = record(value);
  requireNumbers(source, ["image_quota_used", "image_quota_reserved", "image_quota_limit", "image_quota_remaining"], ["image_quota_limit", "image_quota_remaining"]);
  return numberRecord(source);
}

export async function clearImageCooldown(accountId: string) {
  await httpRequest<unknown>(`/api/accounts/${encodeURIComponent(accountId)}/clear-image-cooldown`, { method: "POST" });
}

export async function cancelImageTask(taskId: string) {
  await httpRequest<unknown>(`/api/image-tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
}

export async function endUncertainTask(taskId: string) {
  await httpRequest<unknown>(`/api/runtime/tasks/${encodeURIComponent(taskId)}/end`, { method: "POST" });
}

function isImageTask(value: unknown): value is ImageTask {
  if (!value || typeof value !== "object") return false;
  const source = record(value);
  for (const field of ["recovery_attempts", "recovery_max_attempts"]) {
    const item = source[field];
    if (item !== undefined && (typeof item !== "number" || !Number.isInteger(item) || item < 0)) return false;
  }
  for (const field of ["recovery_next_at", "recovery_deadline"]) {
    const item = source[field];
    if (item !== undefined && item !== null && (typeof item !== "number" || !Number.isFinite(item) || item < 0)) return false;
  }
  for (const field of ["recovery_active", "partial_success"]) {
    if (source[field] !== undefined && typeof source[field] !== "boolean") return false;
  }
  return "id" in value && typeof value.id === "string" && "status" in value && typeof value.status === "string" &&
    ["queued", "running", "success", "error", "uncertain", "cancelled"].includes(value.status) &&
    "mode" in value && (value.mode === "generate" || value.mode === "edit") &&
    "created_at" in value && typeof value.created_at === "string" && "updated_at" in value && typeof value.updated_at === "string" &&
    (!("data" in value) || (Array.isArray(value.data) && value.data.every((item: unknown) => !!item && typeof item === "object" && (!("url" in item) || typeof item.url === "string"))));
}

export function parseTasks(value: unknown): ImageTask[] {
  const source = record(value);
  if (!Array.isArray(source.items) || !source.items.every(isImageTask)) throw new Error("图片任务格式不正确");
  return source.items;
}

export async function fetchRuntimeTasks() {
  return parseTasks(await httpRequest<unknown>("/api/runtime/tasks"));
}

export async function createImageTaskBatch(taskIds: string[], files: File[], prompt: string, model: string, size: string, quality: string) {
  const fields = { client_task_id: `batch:${taskIds[0]}`, client_task_ids: taskIds, prompt, model, size, quality };
  let body: FormData | typeof fields = fields;
  if (files.length) {
    const form = new FormData();
    for (const [key, value] of Object.entries(fields)) form.append(key, Array.isArray(value) ? JSON.stringify(value) : value);
    files.forEach((file) => form.append("image", file));
    body = form;
  }
  return parseTasks(await httpRequest<unknown>(`/api/image-tasks/${files.length ? "edits" : "generations"}`, { method: "POST", body }));
}
