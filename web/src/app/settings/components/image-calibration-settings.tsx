"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { testImageCalibration, type ImageCalibrationSettings } from "@/lib/api";
import { useSettingsStore } from "../store";

export const DEFAULT_IMAGE_CALIBRATION: ImageCalibrationSettings = {
  enabled: false, provider: "worker", worker_url: "http://127.0.0.1:3310", timeout_secs: 300,
};

export function ImageCalibrationSettingsCard() {
  const settings = useSettingsStore((state) => state.config?.image_calibration) ?? DEFAULT_IMAGE_CALIBRATION;
  const update = useSettingsStore((state) => state.setImageCalibrationField);
  const saving = useSettingsStore((state) => state.isSavingConfig);
  const [testing, setTesting] = useState(false);
  const isFal = settings.provider === "fal";
  const target = isFal ? `fal:${settings.fal_key_source ?? "missing"}` : settings.worker_url;
  const clearKey = settings.fal_api_key === null;
  const keyChanged = isFal && (clearKey || Boolean(settings.fal_api_key?.trim()));
  const keyPlaceholder = settings.fal_key_source === "settings" ? "已保存，留空保留" : "粘贴 fal API 密钥";
  const [result, setResult] = useState<{ target: string; message: string } | null>(null);

  function changeKey(value: string | null | undefined) {
    setResult(null);
    update("fal_api_key", value);
  }

  async function testConnection() {
    setTesting(true);
    setResult(null);
    try {
      const response = await testImageCalibration(settings);
      setResult({ target, message: response.message });
    } catch (error) {
      const message = error instanceof Error ? error.message : "连接检查失败";
      setResult({ target, message });
    } finally {
      setTesting(false);
    }
  }

  return (
    <section className="space-y-3 rounded-xl border border-stone-200 p-4 md:col-span-2" aria-label="图片分辨率校准">
      <div className="flex flex-col items-start gap-3 sm:flex-row sm:justify-between">
        <div className="space-y-1">
          <label className="flex items-center gap-2 text-sm font-medium">
            <Checkbox checked={settings.enabled} onCheckedChange={(value) => update("enabled", value === true)} />
            图片分辨率校准
          </label>
          <p className="text-xs leading-5 text-stone-500">图片偏小时自动放大，处理失败时保留原图。</p>
        </div>
        <Button type="button" variant="outline" disabled={testing || saving || keyChanged} onClick={() => void testConnection()}>
          {testing ? "检查中…" : isFal ? "检查 fal 密钥配置" : "检查超分连接"}
        </Button>
      </div>
      <div role="status" className="text-sm text-stone-700 empty:sr-only">
        {result?.target === target ? result.message : null}
      </div>
      {isFal && (
        <div className="space-y-2">
          <label className="block space-y-1 text-sm">
            <span>fal API 密钥</span>
            <Input type="password" autoComplete="new-password" spellCheck={false} maxLength={512}
              aria-describedby="fal-api-key-hint" placeholder={keyPlaceholder} value={settings.fal_api_key ?? ""}
              disabled={saving || testing || clearKey} onChange={(event) => changeKey(event.target.value)} />
          </label>
          <p id="fal-api-key-hint" className="text-xs leading-5 text-stone-500">
            密钥更改在点击下方「保存」后生效。
          </p>
          {settings.fal_key_source === "settings" && (
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={clearKey} disabled={saving || testing}
                onCheckedChange={(value) => changeKey(value === true ? null : undefined)} />
              清除已保存的密钥
            </label>
          )}
        </div>
      )}
      <details className="border-t border-stone-200 pt-2">
        <summary className="cursor-pointer rounded-md py-1 text-sm text-stone-700 focus-visible:outline-2 focus-visible:outline-offset-2">
          超分服务与超时 · {isFal ? "fal.ai" : "Worker"}
        </summary>
        <div className="mt-2 grid gap-3 sm:grid-cols-[minmax(0,1fr)_160px]">
          <label className="block space-y-1 text-sm">
            <span>超分服务</span>
            <select className="block h-9 w-full rounded-md border border-input bg-background px-3" value={settings.provider ?? "worker"}
              onChange={(event) => update("provider", event.target.value === "fal" ? "fal" : "worker")}>
              <option value="worker">自托管 Worker</option>
              <option value="fal">fal.ai · Real-ESRGAN</option>
            </select>
          </label>
          <label className="block space-y-1 text-sm">
            <span>超分总超时（秒）</span>
            <Input type="number" min={1} max={600} step={1} value={settings.timeout_secs} onChange={(event) => update("timeout_secs", Number(event.target.value))} />
          </label>
          {isFal ? (
            <p className="text-xs leading-5 text-stone-500 sm:col-span-2">需要 AI 放大时，图片会发送给 fal 并按推理时间计费。</p>
          ) : (
            <label className="block space-y-1 text-sm sm:col-span-2">
              <span>超分服务地址</span>
              <Input value={settings.worker_url} onChange={(event) => {
                setResult(null);
                update("worker_url", event.target.value);
              }} />
            </label>
          )}
        </div>
      </details>
    </section>
  );
}
