export const DEFAULT_ACCOUNT_PRIORITY = 0;
export const DEFAULT_ACCOUNT_WEIGHT = 1;
export const MAX_ACCOUNT_PRIORITY = 1000;
export const MAX_ACCOUNT_WEIGHT = 1000;

function parseInteger(value: unknown, label: string, minimum: number, maximum: number): number {
  const number = typeof value === "number"
    ? value
    : typeof value === "string" && /^\d+$/.test(value.trim())
      ? Number(value.trim())
      : Number.NaN;
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) {
    throw new Error(`${label}请输入 ${minimum}–${maximum} 之间的整数`);
  }
  return number;
}

export function parseAccountScheduling(priority: unknown, weight: unknown) {
  return {
    priority: parseInteger(priority, "优先级", 0, MAX_ACCOUNT_PRIORITY),
    weight: parseInteger(weight, "权重", 1, MAX_ACCOUNT_WEIGHT),
  };
}
