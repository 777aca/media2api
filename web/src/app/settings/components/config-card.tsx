"use client";

import { Cloud, Copy, LoaderCircle, RefreshCw, Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { ImageStorageMode, ImageStorageProvider } from "@/lib/api";

import { useSettingsStore } from "../store";
import { ImageCalibrationSettingsCard } from "./image-calibration-settings";

const sectionTitles = {
  pool: "号池维护",
  images: "图片生成",
  storage: "图片存储",
  requests: "请求与日志",
};

export function ConfigCard({ section = "pool" }: { section?: keyof typeof sectionTitles }) {
  const logLevelOptions = ["debug", "info", "warning", "error"];
  const config = useSettingsStore((state) => state.config);
  const isLoadingConfig = useSettingsStore((state) => state.isLoadingConfig);
  const isSavingConfig = useSettingsStore((state) => state.isSavingConfig);
  const setRefreshAccountIntervalMinute = useSettingsStore((state) => state.setRefreshAccountIntervalMinute);
  const setImageRetentionHours = useSettingsStore((state) => state.setImageRetentionHours);
  const setImagePollTimeoutSecs = useSettingsStore((state) => state.setImagePollTimeoutSecs);
  const setImageSettleEnabled = useSettingsStore((state) => state.setImageSettleEnabled);
  const setImageRemoveConversationAfterResult = useSettingsStore((state) => state.setImageRemoveConversationAfterResult);
  const setImageRemoveConversationAlways = useSettingsStore((state) => state.setImageRemoveConversationAlways);
  const setImageSettleSecs = useSettingsStore((state) => state.setImageSettleSecs);
  const setImageTimeoutRetrySecs = useSettingsStore((state) => state.setImageTimeoutRetrySecs);
  const setAutoRemoveInvalidAccounts = useSettingsStore((state) => state.setAutoRemoveInvalidAccounts);
  const setAutoRemoveRateLimitedAccounts = useSettingsStore((state) => state.setAutoRemoveRateLimitedAccounts);
  const setAutoReloginAfterRefresh = useSettingsStore((state) => state.setAutoReloginAfterRefresh);
  const setLogLevel = useSettingsStore((state) => state.setLogLevel);
  const setBaseUrl = useSettingsStore((state) => state.setBaseUrl);
  const setGlobalSystemPrompt = useSettingsStore((state) => state.setGlobalSystemPrompt);
  const setDefaultUpstreamModelName = useSettingsStore((state) => state.setDefaultUpstreamModelName);
  const setDefaultThinkingEffort = useSettingsStore((state) => state.setDefaultThinkingEffort);
  const setSensitiveWordsText = useSettingsStore((state) => state.setSensitiveWordsText);
  const setAIReviewField = useSettingsStore((state) => state.setAIReviewField);
  const setImageStorageField = useSettingsStore((state) => state.setImageStorageField);
  const copyBackupR2ToImageStorage = useSettingsStore((state) => state.copyBackupR2ToImageStorage);
  const testImageStorage = useSettingsStore((state) => state.testImageStorage);
  const syncImagesToWebDAV = useSettingsStore((state) => state.syncImagesToWebDAV);
  const isTestingImageStorage = useSettingsStore((state) => state.isTestingImageStorage);
  const isSyncingImageStorage = useSettingsStore((state) => state.isSyncingImageStorage);
  const saveConfig = useSettingsStore((state) => state.saveConfig);

  if (isLoadingConfig) {
    return (
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="flex items-center justify-center p-10">
          <LoaderCircle className="size-5 animate-spin text-stone-400" />
        </CardContent>
      </Card>
    );
  }

  const storageProvider: ImageStorageProvider = config?.image_storage?.provider === "s3" ? "s3" : "webdav";
  const remoteLabel = storageProvider === "s3" ? "R2 / S3" : "WebDAV";
  const remoteModeValue: ImageStorageMode = storageProvider === "s3" ? "s3" : "webdav";
  const currentModeValue: ImageStorageMode = config?.image_storage?.mode === "both"
    ? "both"
    : config?.image_storage?.mode === "webdav" || config?.image_storage?.mode === "s3"
      ? remoteModeValue
      : "local";
  const hasBackupR2 = Boolean(
    config?.backup?.account_id || config?.backup?.access_key_id || config?.backup?.bucket,
  );

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-4 p-6">
        <div className="space-y-1">
          <h2 className="text-lg font-semibold">{sectionTitles[section]}</h2>
          {section === "pool" && <p className="text-sm text-stone-500">管理 ChatGPT / Codex 账号的自动刷新与异常处理。Grok 独立配置在 Grok 页签。</p>}
        </div>
        {section === "images" && <ImageCalibrationSettingsCard />}
        <fieldset disabled={!config || isSavingConfig} className="grid items-start gap-4 md:grid-cols-2">
          {section === "pool" && <>
          <div className="space-y-2">
            <label htmlFor="settings-refresh-interval" className="text-sm text-stone-700">账号刷新间隔</label>
            <Input
              id="settings-refresh-interval"
              value={String(config?.refresh_account_interval_minute || "")}
              onChange={(event) => setRefreshAccountIntervalMinute(event.target.value)}
              placeholder="分钟"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">单位分钟，控制账号自动刷新频率。</p>
          </div>
          <div className="space-y-2">
            <div className="flex-1 space-y-2">
              <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
                <Checkbox
                  checked={Boolean(config?.auto_relogin_after_refresh)}
                  onCheckedChange={(checked) => setAutoReloginAfterRefresh(Boolean(checked))}
                />
                自动重新登录异常账号
              </label>
              <p className="text-xs text-stone-500">开启后刷新时自动尝试密码登录恢复账号。</p>
            </div>
          </div>
          <div className="space-y-2">
            <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
              <Checkbox
                checked={Boolean(config?.auto_remove_invalid_accounts)}
                onCheckedChange={(checked) => setAutoRemoveInvalidAccounts(Boolean(checked))}
              />
              自动移除异常账号
            </label>
            <p className="text-xs text-stone-500">刷新时检测并移除</p>
          </div>
          <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
            <Checkbox
              checked={Boolean(config?.auto_remove_rate_limited_accounts)}
              onCheckedChange={(checked) => setAutoRemoveRateLimitedAccounts(Boolean(checked))}
            />
            自动移除限流账号
          </label>
          </>}
          {section === "images" && <>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">图片访问地址</label>
            <Input
              value={String(config?.base_url || "")}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="https://example.com"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">用于生成图片结果的访问前缀地址。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">默认请求上游模型名称</label>
            <Input
              value={String(config?.default_upstream_model_name || "")}
              onChange={(event) => setDefaultUpstreamModelName(event.target.value)}
              placeholder="gpt-5-5"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">gpt-image-2 发起图片请求时使用的上游模型名称，默认 gpt-5-5。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">默认思考强度</label>
            <Select
              value={String(config?.default_thinking_effort || "auto")}
              onValueChange={(value) => setDefaultThinkingEffort(value as "auto" | "standard" | "extended" | "max")}
            >
              <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">Auto（不传递）</SelectItem>
                <SelectItem value="standard">Standard</SelectItem>
                <SelectItem value="extended">Extended</SelectItem>
                <SelectItem value="max">Max</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-stone-500">模型名称以 -standard、-extended 或 -max 结尾时，模型后缀优先。</p>
          </div>
          <div className="space-y-2">
            <label htmlFor="settings-image-retention-hours" className="text-sm text-stone-700">图片有效期（小时）</label>
            <Input
              id="settings-image-retention-hours"
              type="number"
              min={1}
              max={36500 * 24}
              step={1}
              value={String(config?.image_retention_hours || "")}
              onChange={(event) => setImageRetentionHours(event.target.value)}
              placeholder="360"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">最少 1 小时，24 小时 = 1 天。与图片管理共用设置。到期清理本地图片，保留生成记录和调用日志；不删除 R2/S3 或 WebDAV 远程副本。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">图片轮询超时</label>
            <Input
              value={String(config?.image_poll_timeout_secs || "")}
              onChange={(event) => setImagePollTimeoutSecs(event.target.value)}
              placeholder="120"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">单位秒，等待上游图片结果的最长时间。</p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3">
              <Checkbox
                checked={Boolean(config?.image_settle_enabled !== false)}
                onCheckedChange={(checked) => setImageSettleEnabled(Boolean(checked))}
              />
              <span className="text-sm text-stone-700">图片二次确认机制</span>
            </div>
            <p className="text-xs text-stone-500">打开后能稍微提升获取图片的成功率。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">图片二次确认等待时间</label>
            <Input
              value={String(config?.image_settle_secs || "2.0")}
              onChange={(event) => setImageSettleSecs(event.target.value)}
              placeholder="2.0"
              className="h-10 rounded-xl border-stone-200 bg-white disabled:cursor-not-allowed disabled:opacity-50"
              disabled={!config?.image_settle_enabled}
            />
            <p className="text-xs text-stone-500">单位秒，找到图片后等待多久再次确认。需配合图片二次确认机制使用。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">图片超时继续等待时间</label>
            <Input
              value={String(config?.image_timeout_retry_secs || "30")}
              onChange={(event) => setImageTimeoutRetrySecs(event.target.value)}
              placeholder="360"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">单位秒，超时后点击“继续等待”额外等待的时间。</p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3">
              <Checkbox
                checked={Boolean(config?.image_remove_conversation_after_result)}
                onCheckedChange={(checked) => setImageRemoveConversationAfterResult(Boolean(checked))}
              />
              <span className="text-sm text-stone-700">出图后移除本地对话</span>
            </div>
            <p className="text-xs text-stone-500">成功拿到图片后，异步隐藏 ChatGPT 侧对应的本地对话记录。</p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3">
              <Checkbox
                checked={Boolean(config?.image_remove_conversation_always)}
                onCheckedChange={(checked) => setImageRemoveConversationAlways(Boolean(checked))}
              />
              <span className="text-sm text-stone-700">没出图也移除本地对话</span>
            </div>
            <p className="text-xs text-stone-500">失败、超时或只返回文本时也一并隐藏对话记录（打开后包含出图成功的情况）。</p>
          </div>
          </>}
          {section === "storage" && <>
          <div className="space-y-4 rounded-xl border border-stone-200 bg-white px-4 py-3 md:col-span-2">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <label className="flex items-center gap-3 text-sm text-stone-700">
                <Checkbox
                  checked={Boolean(config?.image_storage?.enabled)}
                  onCheckedChange={(checked) => setImageStorageField("enabled", Boolean(checked))}
                />
                启用远程图片存储（Cloudflare R2 / S3 / WebDAV）
              </label>
              <div className="flex flex-wrap gap-2">
                {storageProvider === "s3" && hasBackupR2 ? (
                  <Button
                    type="button"
                    variant="outline"
                    className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                    onClick={() => copyBackupR2ToImageStorage()}
                  >
                    <Copy className="size-4" />
                    复用备份 R2 配置
                  </Button>
                ) : null}
                <Button
                  type="button"
                  variant="outline"
                  className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                  onClick={() => void testImageStorage()}
                  disabled={isTestingImageStorage || !config?.image_storage?.enabled}
                >
                  {isTestingImageStorage ? <LoaderCircle className="size-4 animate-spin" /> : <Cloud className="size-4" />}
                  测试 {remoteLabel}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                  onClick={() => void syncImagesToWebDAV()}
                  disabled={isSyncingImageStorage || !config?.image_storage?.enabled || config?.image_storage?.mode === "local"}
                >
                  {isSyncingImageStorage ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                  全量同步
                </Button>
              </div>
            </div>
            <p className="text-xs leading-6 text-stone-500">
              生成时实时上传新图片到 {remoteLabel}；全量同步用于把已有本地图片补传到 {remoteLabel}。配置公开访问域名后，生图接口将直接返回公开图片链接。
            </p>
            <div className="rounded-lg border border-stone-100 bg-stone-50 px-3 py-2 text-xs text-stone-600">
              当前待保存模式：
              <span className="ml-1 font-medium text-stone-900">
                {config?.image_storage?.enabled
                  ? config.image_storage.mode === "both"
                    ? `本机 + ${remoteLabel}`
                    : config.image_storage.mode === "webdav" || config.image_storage.mode === "s3"
                      ? `仅 ${remoteLabel}`
                      : "仅本机"
                  : "仅本机"}
              </span>
              <span className="ml-2 text-stone-400">修改后需要点保存，或通过测试/同步按钮自动保存。</span>
            </div>
            <div className="grid gap-4 md:grid-cols-3">
              <div className="space-y-2">
                <label className="text-sm text-stone-700">存储类型</label>
                <Select
                  value={storageProvider}
                  onValueChange={(value) => setImageStorageField("provider", value as ImageStorageProvider)}
                  disabled={!config?.image_storage?.enabled}
                >
                  <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white shadow-none">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="s3">Cloudflare R2 / S3</SelectItem>
                    <SelectItem value="webdav">WebDAV</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">保存模式</label>
                <Select
                  value={currentModeValue}
                  onValueChange={(value) => setImageStorageField("mode", value as ImageStorageMode)}
                  disabled={!config?.image_storage?.enabled}
                >
                  <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white shadow-none">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="local">仅本机</SelectItem>
                    <SelectItem value={remoteModeValue}>仅 {remoteLabel}</SelectItem>
                    <SelectItem value="both">本机 + {remoteLabel}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {storageProvider === "s3" ? (
                <>
                  <div className="space-y-2">
                    <label className="text-sm text-stone-700">Bucket 名称</label>
                    <Input
                      value={String(config?.image_storage?.s3_bucket || "")}
                      onChange={(event) => setImageStorageField("s3_bucket", event.target.value)}
                      placeholder="my-images-bucket"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm text-stone-700">Account ID（Cloudflare R2）</label>
                    <Input
                      value={String(config?.image_storage?.s3_account_id || "")}
                      onChange={(event) => setImageStorageField("s3_account_id", event.target.value)}
                      placeholder="32 位 Account ID（填此项可留空 Endpoint）"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm text-stone-700">S3 Endpoint（可选）</label>
                    <Input
                      value={String(config?.image_storage?.s3_endpoint || "")}
                      onChange={(event) => setImageStorageField("s3_endpoint", event.target.value)}
                      placeholder="https://<account_id>.r2.cloudflarestorage.com"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm text-stone-700">Region</label>
                    <Input
                      value={String(config?.image_storage?.s3_region || "auto")}
                      onChange={(event) => setImageStorageField("s3_region", event.target.value)}
                      placeholder="auto"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm text-stone-700">Access Key ID</label>
                    <Input
                      value={String(config?.image_storage?.s3_access_key_id || "")}
                      onChange={(event) => setImageStorageField("s3_access_key_id", event.target.value)}
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm text-stone-700">Secret Access Key</label>
                    <Input
                      type="password"
                      value={String(config?.image_storage?.s3_secret_access_key || "")}
                      onChange={(event) => setImageStorageField("s3_secret_access_key", event.target.value)}
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm text-stone-700">对象前缀（远端目录）</label>
                    <Input
                      value={String(config?.image_storage?.s3_prefix ?? "")}
                      onChange={(event) => setImageStorageField("s3_prefix", event.target.value)}
                      placeholder="media2api/images（留空表示根目录）"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                </>
              ) : (
                <>
                  <div className="space-y-2">
                    <label className="text-sm text-stone-700">远端目录</label>
                    <Input
                      value={String(config?.image_storage?.webdav_root_path || "")}
                      onChange={(event) => setImageStorageField("webdav_root_path", event.target.value)}
                      placeholder="media2api/images"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                  <div className="space-y-2 md:col-span-3">
                    <label className="text-sm text-stone-700">WebDAV URL</label>
                    <Input
                      value={String(config?.image_storage?.webdav_url || "")}
                      onChange={(event) => setImageStorageField("webdav_url", event.target.value)}
                      placeholder="https://example.com/dav"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm text-stone-700">用户名</label>
                    <Input
                      value={String(config?.image_storage?.webdav_username || "")}
                      onChange={(event) => setImageStorageField("webdav_username", event.target.value)}
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                  <div className="space-y-2 md:col-span-2">
                    <label className="text-sm text-stone-700">密码</label>
                    <Input
                      type="password"
                      value={String(config?.image_storage?.webdav_password || "")}
                      onChange={(event) => setImageStorageField("webdav_password", event.target.value)}
                      className="h-10 rounded-xl border-stone-200 bg-white"
                      disabled={!config?.image_storage?.enabled}
                    />
                  </div>
                </>
              )}
              <div className="space-y-2 md:col-span-3">
                <label className="text-sm text-stone-700">公开访问域名 / 前缀（Public Base URL）</label>
                <Input
                  value={String(config?.image_storage?.public_base_url || "")}
                  onChange={(event) => setImageStorageField("public_base_url", event.target.value)}
                  placeholder={storageProvider === "s3" ? "https://pub-xxx.r2.dev 或 https://img.example.com" : "https://cdn.example.com/media2api/images"}
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={!config?.image_storage?.enabled}
                />
                <p className="text-xs text-stone-500">
                  {storageProvider === "s3"
                    ? "填入 R2 公开访问域名（如 https://pub-xxx.r2.dev 或绑定的自定义域名）后，生图接口将直接返回公开图片地址（若设置了对象前缀且域名无路径，会自动拼接前缀）；留空时返回本应用 /images/... 代理地址。"
                    : "留空时返回本应用 /images/... 代理地址；填入后直接返回公开图片地址。"}
                </p>
              </div>
            </div>
          </div>
          </>}
          {section === "requests" && <>
          <div className="space-y-3 rounded-xl border border-stone-200 bg-white px-4 py-3">
            <div>
              <label className="text-sm text-stone-700">控制台日志级别</label>
              <p className="mt-1 text-xs text-stone-500">不选择时使用默认 info / warning / error。</p>
            </div>
            <div className="grid grid-cols-2 gap-2">
              {logLevelOptions.map((level) => (
                <label key={level} className="flex items-center gap-2 text-sm capitalize text-stone-700">
                  <Checkbox
                    checked={Boolean(config?.log_levels?.includes(level))}
                    onCheckedChange={(checked) => setLogLevel(level, Boolean(checked))}
                  />
                  {level}
                </label>
              ))}
            </div>
          </div>
          <div className="space-y-2 md:col-span-2">
            <label className="text-sm text-stone-700">全局附加指令</label>
            <Textarea
              value={String(config?.global_system_prompt || "")}
              onChange={(event) => setGlobalSystemPrompt(event.target.value)}
              placeholder="例如：先判断用户提示词是否合规；遇到违法、色情、暴力、仇恨等请求时拒绝回答。"
              className="min-h-28 rounded-xl border-stone-200 bg-white font-mono text-xs shadow-none"
            />
            <p className="text-xs text-stone-500">每次请求都会作为 system 消息注入，可用于审核用户提示词、避免违规内容、统一约束模型行为或固定角色设定。</p>
          </div>
          <div className="space-y-2 md:col-span-2">
            <label className="text-sm text-stone-700">敏感词</label>
            <Textarea
              value={(config?.sensitive_words || []).join("\n")}
              onChange={(event) => setSensitiveWordsText(event.target.value)}
              placeholder="一行一个，命中即拒绝"
              className="min-h-28 rounded-xl border-stone-200 bg-white font-mono text-xs shadow-none"
            />
            <p className="text-xs text-stone-500">只要用户请求包含任意敏感词，就直接返回拒绝。</p>
          </div>
          <div className="space-y-4 rounded-xl border border-stone-200 bg-white px-4 py-3 md:col-span-2">
            <label className="flex items-center gap-3 text-sm text-stone-700">
              <Checkbox
                checked={Boolean(config?.ai_review?.enabled)}
                onCheckedChange={(checked) => setAIReviewField("enabled", Boolean(checked))}
              />
              启用 AI 审核
            </label>
            <p className="text-xs leading-6 text-stone-500">
              开启后会在请求进入生图账号前先调用审核模型，审核不通过会直接拒绝，减少违规提示词触达账号造成风控或封号的风险。
            </p>
            <div className="grid gap-4 md:grid-cols-3">
              <div className="space-y-2">
                <label className="text-sm text-stone-700">Base URL</label>
                <Input value={String(config?.ai_review?.base_url || "")} onChange={(event) => setAIReviewField("base_url", event.target.value)} placeholder="https://api.openai.com" className="h-10 rounded-xl border-stone-200 bg-white" />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">API Key</label>
                <Input value={String(config?.ai_review?.api_key || "")} onChange={(event) => setAIReviewField("api_key", event.target.value)} placeholder="sk-..." className="h-10 rounded-xl border-stone-200 bg-white" />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">Model</label>
                <Input value={String(config?.ai_review?.model || "")} onChange={(event) => setAIReviewField("model", event.target.value)} placeholder="gpt-5.4-mini" className="h-10 rounded-xl border-stone-200 bg-white" />
              </div>
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">审核提示词</label>
              <Textarea value={String(config?.ai_review?.prompt || "")} onChange={(event) => setAIReviewField("prompt", event.target.value)} placeholder="判断用户请求是否允许。只回答 ALLOW 或 REJECT。" className="min-h-24 rounded-xl border-stone-200 bg-white text-xs shadow-none" />
            </div>
          </div>
          </>}
        </fieldset>

        <div className="flex justify-end">
          <Button
            className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
            onClick={() => void saveConfig()}
            disabled={isSavingConfig || !config}
          >
            {isSavingConfig ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
            保存
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
