"use client";

import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { testImageCalibration, type ImageCalibrationSettings } from "@/lib/api";
import { useSettingsStore } from "../store";

export const DEFAULT_IMAGE_CALIBRATION: ImageCalibrationSettings = {
  enabled: false, worker_url: "http://127.0.0.1:3310", timeout_secs: 300,
};

export function ImageCalibrationSettingsCard() {
  const settings = useSettingsStore((state) => state.config?.image_calibration) ?? DEFAULT_IMAGE_CALIBRATION;
  const update = useSettingsStore((state) => state.setImageCalibrationField);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState("");

  async function testConnection() {
    setTesting(true);
    setResult("");
    try {
      const response = await testImageCalibration(settings);
      setResult(response.message);
      if (response.ok) toast.success(response.message);
      else toast.error(response.message);
    } catch (error) {
      const message = error instanceof Error ? error.message : "连接检查失败";
      setResult(message);
      toast.error(message);
    } finally {
      setTesting(false);
    }
  }

  return (
    <section className="space-y-3 rounded-xl border border-stone-200 p-4" aria-label="图片分辨率校准">
      <label className="flex items-center gap-2 text-sm font-medium">
        <Checkbox checked={settings.enabled} onCheckedChange={(value) => update("enabled", value === true)} />
        图片分辨率校准
      </label>
      <p className="text-xs leading-5 text-stone-500">返回图片偏小时自动缩放或 AI 超分；保留完整画面和透明背景。失败时保留原图，不重复生成。修改后点击下方“保存”生效。</p>
      <div className="grid gap-3 sm:grid-cols-[1fr_160px_auto] sm:items-end">
        <label className="space-y-1 text-sm">超分服务地址
          <Input aria-label="超分服务地址" value={settings.worker_url} onChange={(event) => update("worker_url", event.target.value)} />
        </label>
        <label className="space-y-1 text-sm">总超时（秒）
          <Input aria-label="超分总超时" type="number" min={1} max={600} step={1} value={settings.timeout_secs} onChange={(event) => update("timeout_secs", Number(event.target.value))} />
        </label>
        <Button type="button" variant="outline" disabled={testing} onClick={() => void testConnection()}>{testing ? "检查中…" : "检查超分连接"}</Button>
      </div>
      {result && <p role="status" className="text-xs text-stone-500">{result}</p>}
    </section>
  );
}
