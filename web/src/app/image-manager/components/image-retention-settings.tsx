"use client";

import { useCallback, useEffect, useState } from "react";
import { Clock3, LoaderCircle, Save } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchImageRetention, updateImageRetention } from "@/lib/api";
import { IMAGE_RETENTION_MAX_HOURS, parseImageRetentionHours, formatImageRetention } from "@/lib/image-retention";

export function ImageRetentionSettings({ onSaved }: { onSaved: () => void }) {
  const [savedHours, setSavedHours] = useState<number | null>(null);
  const [hours, setHours] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const value = await fetchImageRetention();
      setSavedHours(value);
      setHours(String(value));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "加载图片有效期失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const save = async () => {
    setError("");
    setSaving(true);
    try {
      const value = await updateImageRetention(parseImageRetentionHours(hours));
      setSavedHours(value);
      setHours(String(value));
      toast.success(`图片有效期已保存为 ${formatImageRetention(value)}`);
      onSaved();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存图片有效期失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-xl border border-stone-200 bg-white/80 p-4">
      <form className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <div className="space-y-1.5">
          <h2 className="flex items-center gap-2 text-sm font-medium text-stone-800">
            <Clock3 className="size-4" />图片有效期
          </h2>
          <p className="text-xs leading-5 text-stone-500">
            {savedHours === null ? "读取当前有效期后可修改。" : `当前保留 ${formatImageRetention(savedHours)}。`}
            到期自动清理本地图片及无远程副本的缩略图，保留生成记录和调用日志。
          </p>
          <p id="image-retention-hint" className="text-xs leading-5 text-stone-500">
            最少 1 小时，24 小时 = 1 天。保存后适用于已有和新增图片，按文件保存时间计算；每分钟及加载列表时检查。WebDAV 远程副本不受影响。
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <label htmlFor="image-retention-hours" className="text-sm text-stone-600">保留</label>
          <Input id="image-retention-hours" aria-describedby="image-retention-hint" aria-invalid={Boolean(error)}
            type="number" min={1} max={IMAGE_RETENTION_MAX_HOURS} step={1} required
            value={hours} onChange={(event) => setHours(event.target.value)} disabled={loading || saving || savedHours === null}
            className="h-9 w-24 rounded-lg border-stone-200 bg-white" />
          <span className="text-sm text-stone-600">小时</span>
          <Button type="submit" size="sm" disabled={loading || saving || savedHours === null || hours === String(savedHours)}>
            {loading || saving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
            {saving ? "保存中" : "保存有效期"}
          </Button>
        </div>
      </form>
      {error ? <div role="alert" className="mt-3 flex items-center gap-3 text-sm text-rose-600">
        {error}
        {savedHours === null ? <Button type="button" size="sm" variant="outline" disabled={loading} onClick={() => void load()}>重试</Button> : null}
      </div> : null}
    </div>
  );
}
