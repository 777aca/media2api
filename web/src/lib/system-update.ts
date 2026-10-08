import { httpRequest } from "@/lib/request";

export type UpdateJob = {
  id: string; operation: "update" | "rollback"; version: string;
  state: string; message: string; updated_at: number;
};
export type SystemUpdate = {
  current_version: string;
  latest: { version: string; url: string; body: string; published_at: string } | null;
  has_update: boolean; checked_at: number | null; error: string | null;
  supported: boolean; reason: string; deployment: "docker" | "source";
  job: UpdateJob | null; rollback_version: string | null;
};

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("更新状态格式无效");
  return Object.fromEntries(Object.entries(value));
}
function string(value: unknown): string {
  if (typeof value !== "string") throw new Error("更新状态字段无效");
  return value;
}
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("更新状态字段无效");
  return value;
}
function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error("更新时间格式无效");
  return value;
}

export function parseSystemUpdate(value: unknown): SystemUpdate {
  const data = record(value);
  const latest = data.latest === null ? null : record(data.latest);
  const job = data.job === null ? null : record(data.job);
  if (data.deployment !== "docker" && data.deployment !== "source") throw new Error("部署类型无效");
  if (job && job.operation !== "update" && job.operation !== "rollback") throw new Error("更新操作无效");
  return {
    current_version: string(data.current_version),
    latest: latest ? { version: string(latest.version), url: string(latest.url), body: string(latest.body), published_at: string(latest.published_at) } : null,
    has_update: boolean(data.has_update), checked_at: data.checked_at === null ? null : number(data.checked_at),
    error: data.error === null ? null : string(data.error), supported: boolean(data.supported),
    reason: string(data.reason), deployment: data.deployment,
    job: job ? { id: string(job.id), operation: job.operation === "rollback" ? "rollback" : "update", version: string(job.version), state: string(job.state), message: string(job.message), updated_at: number(job.updated_at) } : null,
    rollback_version: data.rollback_version === null ? null : string(data.rollback_version),
  };
}

export function isUpdateRunning(job: UpdateJob | null): boolean {
  return Boolean(job && !["succeeded", "rolled_back", "failed"].includes(job.state));
}

export async function fetchSystemUpdate(force = false, statusOnly = false) {
  const path = statusOnly ? "/status" : force ? "/check" : "";
  return parseSystemUpdate(await httpRequest<unknown>(`/api/system/update${path}`, {
    method: force && !statusOnly ? "POST" : "GET", redirectOnUnauthorized: false,
  }));
}
export async function submitSystemUpdate(operation: "update" | "rollback", version: string) {
  return parseSystemUpdate(await httpRequest<unknown>(`/api/system/update/${operation === "update" ? "apply" : "rollback"}`, {
    method: "POST", body: { version }, redirectOnUnauthorized: false,
  }));
}
