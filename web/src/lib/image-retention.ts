export const IMAGE_RETENTION_MAX_HOURS = 36500 * 24;

export function parseImageRetentionHours(value: unknown): number {
  const hours = typeof value === "string" && /^\d+$/.test(value.trim())
    ? Number(value.trim())
    : value;
  if (typeof hours !== "number" || !Number.isInteger(hours) || hours < 1 || hours > IMAGE_RETENTION_MAX_HOURS) {
    throw new Error(`图片有效期必须是 1 至 ${IMAGE_RETENTION_MAX_HOURS} 的整数（小时）`);
  }
  return hours;
}

export function parseImageRetentionResponse(value: unknown): number {
  if (!value || typeof value !== "object" || !("config" in value)) {
    throw new Error("图片有效期配置格式异常");
  }
  const config = value.config;
  if (!config || typeof config !== "object") {
    throw new Error("图片有效期配置格式异常");
  }
  if ("image_retention_hours" in config) {
    return parseImageRetentionHours(config.image_retention_hours);
  }
  if ("image_retention_days" in config) {
    return parseImageRetentionHours(parseImageRetentionHours(config.image_retention_days) * 24);
  }
  throw new Error("图片有效期配置格式异常");
}

export function formatImageRetention(hours: number): string {
  return hours % 24 === 0 ? `${hours} 小时（${hours / 24} 天）` : `${hours} 小时`;
}
