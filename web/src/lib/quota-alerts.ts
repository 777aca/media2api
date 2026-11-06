export type QuotaAlertSettings = {
  enabled: boolean;
  webhook_url: string;
  secret: string;
  threshold: number;
  repeat_minutes: number;
  has_webhook: boolean;
  has_secret: boolean;
};

export type QuotaAlertStatus = {
  last_success_at: number | null;
  last_error: string | null;
  check_interval_seconds: number;
};

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("通知配置响应格式错误");
  return value as Record<string, unknown>;
}

export function parseQuotaAlerts(value: unknown): { settings: QuotaAlertSettings; status: QuotaAlertStatus } {
  const data = record(value);
  const settings = record(data.settings);
  const status = record(data.status);
  if (typeof settings.enabled !== "boolean" || typeof settings.has_webhook !== "boolean" || typeof settings.has_secret !== "boolean"
    || typeof settings.threshold !== "number" || !Number.isInteger(settings.threshold) || settings.threshold < 0 || settings.threshold > 10000
    || typeof settings.repeat_minutes !== "number" || !Number.isInteger(settings.repeat_minutes) || settings.repeat_minutes < 15 || settings.repeat_minutes > 10080
    || !(status.last_success_at === null || (typeof status.last_success_at === "number" && Number.isFinite(status.last_success_at) && status.last_success_at >= 0))
    || !(status.last_error === null || typeof status.last_error === "string")
    || typeof status.check_interval_seconds !== "number" || status.check_interval_seconds <= 0) {
    throw new Error("通知配置响应格式错误");
  }
  return {
    settings: { enabled: settings.enabled, has_webhook: settings.has_webhook, has_secret: settings.has_secret,
      webhook_url: "", secret: "", threshold: settings.threshold, repeat_minutes: settings.repeat_minutes },
    status: { last_success_at: status.last_success_at as number | null, last_error: status.last_error as string | null,
      check_interval_seconds: status.check_interval_seconds },
  };
}

export function quotaAlertPayload(settings: QuotaAlertSettings, threshold: string, repeatMinutes: string, clearSecret: boolean) {
  if (!/^\d+$/.test(threshold) || Number(threshold) > 10000) throw new Error("号池总剩余配额阈值请输入 0 至 10000 的整数");
  if (!/^\d+$/.test(repeatMinutes) || Number(repeatMinutes) < 15 || Number(repeatMinutes) > 10080) throw new Error("重复提醒间隔请输入 15 至 10080 分钟");
  return { enabled: settings.enabled, webhook_url: settings.webhook_url.trim(), secret: settings.secret.trim(),
    threshold: Number(threshold), repeat_minutes: Number(repeatMinutes), clear_secret: clearSecret };
}
