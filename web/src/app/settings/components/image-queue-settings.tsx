"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { DEFAULT_QUEUE, type QueueSettings } from "@/lib/generation-runtime";
import { useSettingsStore } from "../store";

const fields: Array<{ key: keyof QueueSettings; label: string; min: number; max?: number }> = [
  { key: "global_concurrency", label: "全局同时处理图片数", min: 1 },
  { key: "key_concurrency", label: "默认调用 Key 并发", min: 1 },
  { key: "max_waiting_images", label: "最多等待图片数", min: 1, max: 10000 },
  { key: "queue_timeout_seconds", label: "排队超时（秒）", min: 1, max: 2592000 },
  { key: "task_retention_days", label: "任务输入保留天数", min: 30, max: 3650 },
];

export function ImageQueueSettings() {
  const config = useSettingsStore((state) => state.config);
  const saveConfig = useSettingsStore((state) => state.saveConfig);
  const isSavingConfig = useSettingsStore((state) => state.isSavingConfig);
  const setImageAccountConcurrency = useSettingsStore((state) => state.setImageAccountConcurrency);
  const settings = { ...DEFAULT_QUEUE, ...config?.image_queue };
  return <Card className="mt-4"><CardContent className="space-y-4 p-6">
    <h2 className="text-lg font-semibold">生图调度</h2>
    <p className="text-sm text-muted-foreground">网页和兼容 API 共用一个等待队列。全局、调用 Key 和上游账号都有名额时开始执行；结果保存及后处理完成后释放名额。</p>
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      <label className="space-y-2 text-sm"><span>上游账号并发</span><Input type="number" min={1} step={1} value={config?.image_account_concurrency ?? 3} disabled={!config}
        onChange={(event) => setImageAccountConcurrency(event.target.value)} /><span className="block text-xs text-muted-foreground">每个 ChatGPT / Codex 账号的并发上限，沿用已有配置。</span></label>
      {fields.map(({ key, label, min, max }) => <label key={key} className="space-y-2 text-sm">
        <span>{label}</span><Input type="number" min={min} max={max} step={1} value={settings[key]} disabled={!config}
          onChange={(event) => { if (config) useSettingsStore.setState({ config: { ...config, image_queue: { ...settings, [key]: Number(event.target.value) } } }); }} />
      </label>)}
    </div>
    <p className="text-xs text-muted-foreground">降低并发后已开始的任务继续执行。未结束任务的输入始终保留；幂等标识至少保留 30 天。</p>
    <Button disabled={!config || isSavingConfig} onClick={() => { void saveConfig(); }}>{isSavingConfig ? "保存中…" : "保存配置"}</Button>
  </CardContent></Card>;
}
