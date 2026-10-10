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

const image25QualityOptions: readonly ImageQualityOption[] = [
  ...standardQualityOptions,
  { value: "xhigh", label: "超高" },
  { value: "max", label: "最高" },
];

export function getImageQualityOptions(model: string): readonly ImageQualityOption[] {
  return model === "gpt-image-2.5-flare" || model === "gpt-image-2.5-sunburst"
    ? image25QualityOptions
    : standardQualityOptions;
}

export function normalizeImageQuality(model: string, quality: unknown): string {
  return getImageQualityOptions(model).find((option) => option.value === quality)?.value || "auto";
}
