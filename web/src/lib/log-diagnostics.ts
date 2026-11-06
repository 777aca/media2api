import type { SystemLog } from "./api";

export function logRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function logLabel(labels: Record<string, string>, value: unknown, fallback = "未知") {
  const key = logText(value, "");
  return Object.hasOwn(labels, key) && typeof labels[key] === "string" ? labels[key] : logText(value, fallback);
}

export function parseLogResponse(value: unknown): { items: SystemLog[]; total: number } {
  const data = logRecord(value);
  if (!Array.isArray(data.items)) throw new Error("日志响应格式无效");
  const items = data.items.flatMap((value: unknown): SystemLog[] => {
    const row = logRecord(value);
    if (typeof row.id !== "string" || typeof row.time !== "string" || typeof row.type !== "string") return [];
    return [{ id: row.id, time: row.time, type: row.type, summary: typeof row.summary === "string" ? row.summary : undefined, detail: logRecord(row.detail) }];
  });
  const total = typeof data.total === "number" && Number.isSafeInteger(data.total) && data.total >= 0 ? data.total : items.length;
  return { items, total };
}

export function logText(value: unknown, fallback = "未知") {
  return (typeof value === "string" && value.trim()) || (typeof value === "number" && Number.isFinite(value)) ? String(value) : fallback;
}

export function logNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

export const kindLabels: Record<string, string> = { chat: "对话", image: "图片", video: "视频", ppt: "PPT", psd: "PSD", search: "搜索", other: "其他" };
export const logChannelLabels: Record<string, string> = { web: "ChatGPT Web", chatgpt: "ChatGPT Web", chatgpt_web: "ChatGPT Web", codex: "Codex", grok: "Grok Web", grok_web: "Grok Web" };
export const phaseLabels: Record<string, string> = { preparing: "准备请求", queued: "排队", account_selected: "选择账号", uploading: "上传素材", uploaded: "素材已上传", submitting: "提交生成", submitted: "已提交", streaming: "接收结果", downloading: "下载结果", raw_saved: "保存原图", processing: "处理结果", completed: "完成", failed: "失败", finished: "结束" };
export const errorLabels: Record<string, string> = { quota_exhausted: "额度不足", rate_limited: "请求限流", authentication: "身份验证失败", auth: "登录失效", timeout: "请求超时", upload_failed: "素材上传失败", model_permission_denied: "无模型权限", no_account: "无可用账号", upstream: "上游错误", upstream_error: "上游错误", content_filter: "内容限制", cancelled: "已取消" };

export function logKind(detail: Record<string, unknown>): string {
  if (typeof detail.task_type === "string" && Object.hasOwn(kindLabels, detail.task_type)) return detail.task_type;
  const endpoint = logText(detail.endpoint, "");
  for (const kind of ["ppt", "psd", "search", "video", "image"]) {
    if (endpoint.includes(`/${kind}`)) return kind;
  }
  if (/\/(chat|responses|messages|conversations)/.test(endpoint)) return "chat";
  return "other";
}

export function modelPair(detail: Record<string, unknown>) {
  return { requested: logText(detail.requested_model ?? detail.model), actual: logText(detail.actual_model) };
}

export function logStatus(detail: Record<string, unknown>): { label: string; variant: "success" | "danger" | "warning" | "secondary" } {
  const requested = logNumber(detail.requested_count), output = logNumber(detail.output_count);
  if (detail.status === "partial" || detail.partial_success === true || (requested !== undefined && output !== undefined && output > 0 && output < requested)) return { label: "部分成功", variant: "warning" };
  if (detail.status === "success") return { label: "成功", variant: "success" };
  if (detail.status === "failed" || detail.status === "error") return { label: "失败", variant: "danger" };
  if (detail.status === "cancelled" || detail.status === "canceled") return { label: "已取消", variant: "secondary" };
  return { label: "未知", variant: "secondary" };
}

export function outputCount(detail: Record<string, unknown>) {
  const requested = logNumber(detail.requested_count), output = logNumber(detail.output_count);
  if (output === undefined && requested === undefined) return "未知";
  return `${output ?? "?"} / ${requested ?? "?"}`;
}

export function durationText(value: unknown) {
  const number = logNumber(value);
  return number === undefined ? "未知" : `${(number / 1000).toFixed(2)} s`;
}

export function safeResultUrls(detail: Record<string, unknown>): string[] {
  if (!Array.isArray(detail.urls)) return [];
  return [...new Set(detail.urls.filter((value): value is string => typeof value === "string" && (/^https?:\/\//i.test(value) || /^\/(?!\/)/.test(value)) && !/[\\\u0000-\u001f]/.test(value)))];
}

export type AssetSummary = { path: string; type: string; mime_type?: string; width?: number; height?: number; size_bytes?: number; sha256?: string; decode_status?: string; upload_status?: string };

export function assetSummaries(parameters: unknown): AssetSummary[] {
  const found: AssetSummary[] = [];
  let nodes = 0;
  function visit(value: unknown, path: string, depth: number) {
    if (++nodes > 1000 || depth > 12 || found.length >= 100) return;
    if (Array.isArray(value)) { value.forEach((item, index) => visit(item, `${path}[${index}]`, depth + 1)); return; }
    const item = logRecord(value);
    if (["base64", "file", "binary"].includes(String(item.type)) && item.content_omitted === true) {
      found.push({ path, type: String(item.type), mime_type: typeof (item.mime_type ?? item.content_type) === "string" ? String(item.mime_type ?? item.content_type) : undefined,
        width: logNumber(item.width), height: logNumber(item.height), size_bytes: logNumber(item.size_bytes),
        sha256: typeof item.sha256 === "string" && /^[a-f0-9]{64}$/i.test(item.sha256) ? item.sha256 : undefined,
        decode_status: typeof item.decode_status === "string" ? item.decode_status : undefined,
        upload_status: typeof item.upload_status === "string" ? item.upload_status : undefined });
      return;
    }
    Object.entries(item).forEach(([key, child]) => visit(child, path ? `${path}.${key}` : key, depth + 1));
  }
  visit(parameters, "", 0);
  return found;
}

export function executionRecords(detail: Record<string, unknown>) {
  if (Array.isArray(detail.attempts) && detail.attempts.length) return detail.attempts.map(logRecord);
  const rows = Array.isArray(detail.image_tasks) ? detail.image_tasks.map(logRecord) : [];
  return rows.flatMap(row => Array.isArray(row.attempts) && row.attempts.length
    ? row.attempts.map(value => ({ ...logRecord(value), image_child_id: row.image_child_id })) : [row]);
}

export function inputUploads(detail: Record<string, unknown>) {
  const sources = [detail, ...(Array.isArray(detail.image_tasks) ? detail.image_tasks.map(logRecord) : [])];
  const seen = new Set<string>();
  return sources.flatMap(source => Array.isArray(source.input_uploads) ? source.input_uploads.map(logRecord) : []).filter(row => {
    if (typeof row.upload_id !== "string" || seen.has(row.upload_id)) return false;
    seen.add(row.upload_id); return true;
  });
}
