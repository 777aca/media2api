type ImageQualityOption = {
  value: string;
  label: string;
};

const standardQualityOptions: readonly ImageQualityOption[] = [
  { value: "auto", label: "自动" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

const webImageModelPattern = /^gpt-image-\d+(?:\.\d+)+-[a-z0-9]+(?:-[a-z0-9]+)*$/;

const image25QualityOptions: readonly ImageQualityOption[] = [
  ...standardQualityOptions,
  { value: "xhigh", label: "超高" },
  { value: "max", label: "最高" },
];

export function getImageQualityOptions(model: string): readonly ImageQualityOption[] {
  if (model.startsWith("grok-")) return [{ value: "auto", label: "自动" }];
  // 与后端发现规则一致：gpt-image-N.N-后缀 形式的网页图片型号支持扩展档位。
  return webImageModelPattern.test(model.trim().toLowerCase())
    ? image25QualityOptions
    : standardQualityOptions;
}

export function normalizeImageQuality(model: string, quality: unknown): string {
  return getImageQualityOptions(model).find((option) => option.value === quality)?.value || "auto";
}
