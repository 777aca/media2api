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

function requireNumbers(source: Record<string, unknown>, keys: string[], nullable: string[] = []) {
  for (const key of keys) {
    const value = source[key];
    if (value === null && nullable.includes(key)) continue;
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new Error(`运行数据 ${key} 无效`);
  }
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

export async function createImageTaskBatch(taskIds: string[], files: File[], prompt: string, model: string, size: string, quality: string) {
  if (taskIds.length !== 1) throw new Error("每次请求仅支持生成 1 张图片，不支持批量生成");
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
