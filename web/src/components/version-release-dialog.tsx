"use client";

import { useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import webConfig from "@/constants/common-env";
import { GITHUB_URL } from "@/constants/project";
import { useVersionCheck } from "@/hooks/use-version-check";
import { cn } from "@/lib/utils";

function typeVariant(type: string): "success" | "danger" | "info" | "violet" | "outline" {
  if (type === "新增") return "success";
  if (type === "修复") return "danger";
  if (type === "调整") return "info";
  if (type === "文档") return "violet";
  return "outline";
}

export function VersionReleaseDialog({ className, isAdmin = false }: { className?: string; isAdmin?: boolean }) {
  const [confirmation, setConfirmation] = useState<"update" | "rollback" | null>(null);
  const {
    open,
    setOpen,
    openReleaseModal,
    latestVersion,
    releases,
    checking,
    hasNewVersion,
    checkLatestRelease,
    currentVersion, state, submitting, running, connectionError, submit,
  } = useVersionCheck(isAdmin);
  const error = connectionError || state?.error;
  const targetVersion = confirmation === "rollback" ? state?.rollback_version : state?.latest?.version;

  return (
    <>
      <button
        type="button"
        className={cn(
          "relative px-1 py-1 text-[11px] font-medium text-stone-500 transition hover:text-stone-900 dark:text-stone-300 dark:hover:text-white",
          className,
        )}
        onClick={openReleaseModal}
        title="查看版本更新"
      >
        v{currentVersion}
        {hasNewVersion ? (
          <span className="absolute -top-1 -right-1 size-2 rounded-full bg-emerald-500" />
        ) : null}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90vh] w-[min(94vw,680px)] overflow-y-auto rounded-2xl">
          <DialogHeader>
            <DialogTitle>版本更新</DialogTitle>
            <DialogDescription className="sr-only">查看发布版本、更新进度及回滚操作。</DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <VersionCard label="当前版本" value={currentVersion} />
            <VersionCard
              label="最新版本"
              value={latestVersion}
              action={isAdmin ? (
                <button
                  type="button"
                  disabled={checking || running}
                  className="text-[11px] text-stone-400 underline-offset-2 hover:text-stone-700 hover:underline dark:hover:text-stone-200"
                  onClick={() => void checkLatestRelease(true)}
                >
                  {checking ? "检查中..." : "检查更新"}
                </button>
              ) : null}
            />
          </div>
          {isAdmin ? (
            <div className="space-y-3 text-sm">
              <p className="text-xs text-stone-500">
                {state?.checked_at ? `最近检查：${new Date(state.checked_at * 1000).toLocaleString()}` : "尚未成功检查正式版本"}
              </p>
              {error ? <p role="alert" className="text-amber-700 dark:text-amber-300">{error}</p> : null}
              {state && !state.latest && !state.error ? <p>本项目暂未发布正式版本。</p> : null}
              {state && !state.supported ? (
                <p className="text-stone-500">{state.reason}。<a className="underline" href={`${GITHUB_URL}/blob/main/docs/version-updates.md`} target="_blank" rel="noreferrer">查看启用步骤</a></p>
              ) : null}
              {state?.job ? (
                <div role="status" className="rounded-xl border border-stone-200 p-3 dark:border-white/10">
                  <p className="font-medium">{state.job.operation === "rollback" ? "回滚" : "更新"}至 v{state.job.version}</p>
                  <p className="mt-1 text-stone-500">{state.job.message}</p>
                  {running ? <p className="mt-1 text-xs text-stone-500">任务由独立更新服务执行，可关闭弹窗，重新打开后继续查看。</p> : null}
                </div>
              ) : null}
              <div className="flex flex-wrap gap-2">
                <Button size="sm" disabled={!state?.supported || !hasNewVersion || Boolean(state.error) || running || submitting} onClick={() => setConfirmation("update")}>
                  {running ? "操作进行中…" : "一键更新"}
                </Button>
                <Button size="sm" variant="outline" disabled={!state?.supported || !state.rollback_version || running || submitting} onClick={() => setConfirmation("rollback")}>
                  {state?.rollback_version ? `回滚至 v${state.rollback_version}` : "暂无可回滚版本"}
                </Button>
                {state?.job?.state === "succeeded" && currentVersion !== webConfig.appVersion ? <Button size="sm" variant="outline" onClick={() => window.location.reload()}>刷新页面</Button> : null}
              </div>
              {confirmation && targetVersion ? (
                <div className="space-y-3 rounded-xl bg-stone-100 p-3 dark:bg-white/5">
                  <p>确认{confirmation === "rollback" ? "回滚" : "更新"}至 v{targetVersion}？切换时服务会短暂中断，请在生图任务完成后操作。账号、图片和配置会保留。</p>
                  <div className="flex gap-2">
                    <Button size="sm" disabled={running || submitting || !state?.supported} onClick={() => { const operation = confirmation; setConfirmation(null); void submit(operation, targetVersion); }}>确认并开始</Button>
                    <Button size="sm" variant="outline" onClick={() => setConfirmation(null)}>取消</Button>
                  </div>
                </div>
              ) : null}
            </div>
          ) : <p className="text-sm text-stone-500">登录管理员账号后可检查版本并管理更新。</p>}
          {state?.latest?.body ? <div className="max-h-48 overflow-y-auto whitespace-pre-wrap rounded-xl bg-stone-50 p-3 text-sm dark:bg-white/5">{state.latest.body}</div> : null}
          <div className="max-h-[56vh] space-y-5 overflow-y-auto pr-1">
            {releases.map((release) => (
              <div key={release.version} className="border-l border-stone-200 pl-4 dark:border-white/10">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-stone-950 dark:text-stone-100">
                    {release.version === "Unreleased" ? "未发布" : release.version}
                  </span>
                  <span className="text-xs text-stone-500 dark:text-stone-400">{release.date}</span>
                  {release.version === latestVersion ? <Badge variant="success">最新</Badge> : null}
                  {release.version === currentVersion ? <Badge variant="outline">当前</Badge> : null}
                </div>
                <div className="mt-2 space-y-1.5">
                  {release.items.map((item, index) => (
                    <div key={index} className="flex items-start gap-2 text-sm leading-6 text-stone-700 dark:text-stone-300">
                      <Badge variant={typeVariant(item.type)} className="mt-0.5 shrink-0">
                        {item.type}
                      </Badge>
                      <span className="min-w-0 flex-1">{item.content}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
          <Button variant="outline" size="sm" asChild>
            <a href={`${GITHUB_URL}/releases`} target="_blank" rel="noreferrer">
              查看 GitHub 发布记录
            </a>
          </Button>
        </DialogContent>
      </Dialog>
    </>
  );
}

function VersionCard({
  label,
  value,
  action,
}: {
  label: string;
  value: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-stone-200 bg-white/55 p-3 dark:border-white/10 dark:bg-white/5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs text-stone-500 dark:text-stone-400">{label}</div>
        {action}
      </div>
      <div className="mt-1 text-base font-semibold text-stone-950 dark:text-stone-100">{value}</div>
    </div>
  );
}
