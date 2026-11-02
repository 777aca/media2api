import webConfig from "@/constants/common-env";
import { getStoredAuthKey } from "@/store/auth";

export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export function items(value: unknown, key = "items"): Record<string, unknown>[] {
  const list = record(value)[key];
  return Array.isArray(list) ? list.map(record) : [];
}
export function apiUrl(path: string) { return webConfig.apiUrl.replace(/\/$/, "") + path; }

export async function* chatStream(body: unknown, signal: AbortSignal): AsyncGenerator<string> {
  const key = await getStoredAuthKey();
  const response = await fetch(apiUrl("/v1/chat/completions"), { method: "POST", signal,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` }, body: JSON.stringify(body) });
  if (!response.ok) {
    const error: unknown = await response.json().catch(() => null);
    const payload = record(error);
    throw new Error(String(record(payload.error).message || payload.detail || `请求失败 (${response.status})`));
  }
  if (!response.body) throw new Error("没有收到响应流");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      if (done && buffer) { lines.push(buffer); buffer = ""; }
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") return;
        const event: unknown = JSON.parse(data);
        const e = record(event);
        if (e.error) throw new Error(String(record(e.error).message || "生成失败"));
        const choices = Array.isArray(e.choices) ? e.choices : [];
        const text = record(record(choices[0]).delta).content;
        if (typeof text === "string") yield text;
      }
      if (done) return;
    }
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
}

export async function imageData(file: File): Promise<string> {
  if (!file.type.startsWith("image/") || file.size > 10 * 1024 * 1024) throw new Error("请选择小于 10 MiB 的图片");
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => typeof reader.result === "string" ? resolve(reader.result) : reject(new Error("图片读取失败"));
    reader.onerror = () => reject(new Error("图片读取失败"));
    reader.readAsDataURL(file);
  });
}
