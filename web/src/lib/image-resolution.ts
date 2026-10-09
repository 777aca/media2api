export const IMAGE_SIZE_PRESETS = [
  { ratio: "1:1", tier: "1k", width: "1024", height: "1024", label: "1:1" },
  { ratio: "2:3", tier: "1k", width: "1024", height: "1536", label: "2:3" },
  { ratio: "3:2", tier: "1k", width: "1536", height: "1024", label: "3:2" },
  { ratio: "3:4", tier: "1k", width: "1008", height: "1344", label: "3:4" },
  { ratio: "4:3", tier: "1k", width: "1344", height: "1008", label: "4:3" },
  { ratio: "9:16", tier: "1k", width: "864", height: "1536", label: "9:16" },
  { ratio: "16:9", tier: "1k", width: "1536", height: "864", label: "16:9" },
  { ratio: "1:1", tier: "2k", width: "2048", height: "2048", label: "1:1 (2K)" },
  { ratio: "16:9", tier: "2k", width: "2048", height: "1152", label: "16:9 (2K)" },
  { ratio: "9:16", tier: "2k", width: "1152", height: "2048", label: "9:16 (2K)" },
  { ratio: "16:9", tier: "qhd", width: "2560", height: "1440", label: "16:9 (QHD)" },
  { ratio: "9:16", tier: "qhd", width: "1440", height: "2560", label: "9:16 (QHD)" },
  { ratio: "16:9", tier: "4k", width: "3840", height: "2160", label: "16:9 (4K)" },
  { ratio: "9:16", tier: "4k", width: "2160", height: "3840", label: "9:16 (4K)" },
  { ratio: "auto", tier: "auto", width: "1024", height: "1024", label: "自动" },
];

export function imageSizeError(value: string): string | null {
  if (value === "auto") return null;
  const match = /^(\d{1,4})x(\d{1,4})$/.exec(value);
  if (!match) return "尺寸必须是 auto 或 WIDTHxHEIGHT";
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (Math.min(width, height) <= 0 || Math.max(width, height) > 3840 || width % 16 || height % 16) {
    return "宽高须为 16 的正整数倍，且不超过 3840 像素";
  }
  if (Math.max(width, height) > 3 * Math.min(width, height)) return "长短边比例不能超过 3:1";
  if (width * height < 655360 || width * height > 8294400) return "总像素须在 655,360 至 8,294,400 之间";
  return null;
}

export function isExperimentalSize(width: string, height: string): boolean {
  return Number(width) * Number(height) > 3686400;
}

export type ImageProcessingMetadata = {
  requested_size?: string;
  source_size?: string;
  actual_size?: string;
  processing?: "none" | "resize" | "super_resolution";
  processing_status?: "unchanged" | "applied" | "fallback";
};

export function parseImageProcessing(value: unknown): ImageProcessingMetadata {
  if (typeof value !== "object" || value === null) return {};
  const result: ImageProcessingMetadata = {};
  for (const key of ["requested_size", "source_size", "actual_size"] as const) {
    if (key in value) {
      const size = Reflect.get(value, key);
      if (typeof size === "string" && (size === "auto" || /^[1-9]\d{0,4}x[1-9]\d{0,4}$/.test(size))) result[key] = size;
    }
  }
  if ("processing" in value && (value.processing === "none" || value.processing === "resize" || value.processing === "super_resolution")) result.processing = value.processing;
  if ("processing_status" in value && (value.processing_status === "unchanged" || value.processing_status === "applied" || value.processing_status === "fallback")) result.processing_status = value.processing_status;
  return result;
}

export function imageProcessingLabel(value: ImageProcessingMetadata): string {
  if (value.processing_status === "fallback") return "超分未完成，已保留原图";
  if (value.processing === "super_resolution") return "AI 超分";
  if (value.processing === "resize") return "普通缩放";
  if (value.processing === "none") return "上游原图";
  return "";
}
