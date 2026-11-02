"use client";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { httpRequest } from "@/lib/request";
import { record } from "@/lib/native-media";

export function GrokSettings() {
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const [changes, setChanges] = useState<Record<string, unknown>>({});
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { httpRequest<unknown>("/api/grok/settings").then(v => setSettings(record(v))).catch(e => setStatus(String(e))); }, []);
  async function save() {
    setBusy(true);
    try {
      await httpRequest("/api/grok/settings", { method: "PATCH", body: changes });
      setSettings(record(await httpRequest<unknown>("/api/grok/settings")));
      setChanges({}); setStatus("设置已保存");
    }
    catch (e) { setStatus(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
  }
  const fields = [["signer_url", "Statsig 签名服务 URL"], ["proxy_url", "Grok 代理（留空沿用全局设置）"], ["statsig_value", "手动 Statsig 值（可选）"],
    ["global_concurrency", "对话/视频总并发"], ["user_concurrency", "每个用户并发"], ["account_concurrency", "每个 Grok 账号并发"], ["free_video_duration_cap", "Basic 视频时长上限（秒）"], ["retention_days", "对话与视频保留天数（启动时清理）"]];
  return <section className="my-5 space-y-5 rounded-lg border border-border/80 bg-background p-5"><div className="border-b pb-4"><h2 className="text-sm font-medium">Grok Web 设置</h2><p className="mt-1 text-xs text-muted-foreground">连接与请求管理</p></div>
    <p className="text-sm text-stone-500">签名服务用于 Grok Web 请求签名，仅接收页面元数据，不接收账号 Cookie。代理和手动签名不回显；不修改输入框会保留原值。</p>
    <div className="grid gap-4 md:grid-cols-2">{fields.map(([key,label]) => <label key={key} className="space-y-2 text-sm"><span>{label}</span>
      <Input disabled={busy} type={key.includes("concurrency") || key.includes("cap") || key === "retention_days" ? "number" : key === "proxy_url" || key === "statsig_value" ? "password" : "text"}
        value={String(changes[key] ?? settings[key] ?? "")} onChange={e => setChanges(v => ({ ...v, [key]: e.target.type === "number" ? Number(e.target.value) : e.target.value }))} /></label>)}</div>
    <Button disabled={busy || !Object.keys(changes).length} onClick={() => void save()}>保存 Grok 设置</Button><p role="status" className="text-sm">{status}</p>
  </section>;
}
