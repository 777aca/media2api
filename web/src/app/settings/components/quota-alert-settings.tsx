"use client";

import { useEffect, useState } from "react";
import { BellRing, LoaderCircle, Send } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { httpRequest } from "@/lib/request";
import { parseQuotaAlerts, quotaAlertPayload, type QuotaAlertSettings, type QuotaAlertStatus } from "@/lib/quota-alerts";

export function QuotaAlertSettingsCard() {
  const [settings, setSettings] = useState<QuotaAlertSettings | null>(null);
  const [status, setStatus] = useState<QuotaAlertStatus | null>(null);
  const [threshold, setThreshold] = useState("3");
  const [repeatMinutes, setRepeatMinutes] = useState("60");
  const [clearSecret, setClearSecret] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadVersion, setLoadVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void httpRequest<unknown>("/api/quota-alerts").then((value) => {
      if (cancelled) return;
      const data = parseQuotaAlerts(value);
      setSettings(data.settings);
      setStatus(data.status);
      setThreshold(String(data.settings.threshold));
      setRepeatMinutes(String(data.settings.repeat_minutes));
      setError("");
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "通知配置加载失败");
    });
    return () => { cancelled = true; };
  }, [loadVersion]);

  async function submit(test: boolean) {
    if (!settings || busy) return;
    setBusy(true);
    setError("");
    try {
      const body = quotaAlertPayload(settings, threshold, repeatMinutes, clearSecret);
      const value = await httpRequest<unknown>(test ? "/api/quota-alerts/test" : "/api/quota-alerts", { method: "POST", body });
      if (test) {
        if (!value || typeof value !== "object" || !("ok" in value) || value.ok !== true) throw new Error("飞书未确认测试消息发送成功");
        toast.success("测试消息已发送到飞书群");
      } else {
        const data = parseQuotaAlerts(value);
        setSettings(data.settings);
        setStatus(data.status);
        setClearSecret(false);
        toast.success(data.settings.enabled ? "已启用飞书配额提醒" : "已保存，自动提醒已关闭");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败，请重试");
    } finally { setBusy(false); }
  }

  async function refreshStatus() {
    setBusy(true);
    try {
      setStatus(parseQuotaAlerts(await httpRequest<unknown>("/api/quota-alerts")).status);
      setError("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "状态加载失败"); }
    finally { setBusy(false); }
  }

  return <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm"><CardContent className="space-y-5 p-6">
    <div className="flex items-center gap-2"><BellRing className="size-5 text-amber-600" /><h2 className="text-lg font-semibold">飞书配额提醒</h2></div>
    <p className="text-sm text-stone-500">账号剩余配额达到阈值时推送到飞书群。多个账号合并通知；从低配额变为耗尽时追加提醒。</p>
    {error && <div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
    {!settings ? <div className="flex items-center gap-3 text-sm text-stone-500">{error ? <Button variant="outline" onClick={() => setLoadVersion((value) => value + 1)}>重新加载</Button> : <><LoaderCircle className="size-4 animate-spin" />加载通知配置…</>}</div> : <>
      <label className="flex items-center gap-2 text-sm font-medium"><Checkbox checked={settings.enabled} disabled={busy}
        onCheckedChange={(checked) => setSettings({ ...settings, enabled: checked === true })} />启用飞书机器人通知</label>
      <div className="grid gap-4 md:grid-cols-2">
        <label className="space-y-2 text-sm"><span>机器人 Webhook</span><Input type="password" autoComplete="off" disabled={busy} value={settings.webhook_url}
          placeholder={settings.has_webhook ? "已配置，留空保留原地址" : "https://open.feishu.cn/open-apis/bot/v2/hook/…"}
          onChange={(event) => setSettings({ ...settings, webhook_url: event.target.value })} /></label>
        <label className="space-y-2 text-sm"><span>签名密钥（可选）</span><Input type="password" autoComplete="new-password" disabled={busy || clearSecret} value={settings.secret}
          placeholder={settings.has_secret ? "已配置，留空保留原密钥" : "机器人启用签名校验时填写"}
          onChange={(event) => setSettings({ ...settings, secret: event.target.value })} /></label>
        <label className="space-y-2 text-sm"><span>剩余配额 ≤ 此数值时提醒</span><Input type="number" min={0} max={10000} step={1} disabled={busy} value={threshold} onChange={(event) => setThreshold(event.target.value)} />
          <span className="block text-xs text-stone-500">可自定义，设为 0 只提醒耗尽。次数额度按次判断，Grok 周额度按剩余百分比判断。</span></label>
        <label className="space-y-2 text-sm"><span>重复提醒间隔（分钟）</span><Input type="number" min={15} max={10080} step={1} disabled={busy} value={repeatMinutes} onChange={(event) => setRepeatMinutes(event.target.value)} />
          <span className="block text-xs text-stone-500">持续不足时按此间隔提醒；配额恢复后再次不足会重新提醒。</span></label>
      </div>
      {settings.has_secret && <label className="flex items-center gap-2 text-xs text-stone-500"><Checkbox checked={clearSecret} disabled={busy} onCheckedChange={(value) => setClearSecret(value === true)} />清除已保存的签名密钥</label>}
      <div className="rounded-lg bg-stone-50 p-3 text-xs leading-6 text-stone-500">
        每 30 秒检查本地最新配额；未获取配额、已禁用、异常账号及已过重置时间的旧配额不触发提醒。没有明确配额的 Codex 账号暂不提醒。<br />
        如机器人设置了关键词，请添加 <span className="font-medium text-stone-700">media2api</span>。测试按钮会向群内发送一条测试消息，不保存或启用配置。
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button disabled={busy} onClick={() => void submit(false)}>{busy ? "处理中…" : "保存通知设置"}</Button>
        <Button variant="outline" disabled={busy || (!settings.webhook_url.trim() && !settings.has_webhook)} onClick={() => void submit(true)}><Send className="size-4" />测试推送</Button>
        <Button variant="ghost" disabled={busy} onClick={() => void refreshStatus()}>刷新通知状态</Button>
      </div>
      <div className="text-xs text-stone-500">最近自动推送：{status?.last_success_at ? new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "medium" }).format(new Date(status.last_success_at * 1000)) + " UTC+8" : "暂无"}</div>
      {status?.last_error && <div role="status" className="text-sm text-amber-700">{status.last_error}</div>}
    </>}
  </CardContent></Card>;
}
