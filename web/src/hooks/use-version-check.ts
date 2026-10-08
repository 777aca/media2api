"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import webConfig from "@/constants/common-env";
import { type ReleaseInfo } from "@/lib/release";
import { fetchSystemUpdate, isUpdateRunning, submitSystemUpdate, type SystemUpdate } from "@/lib/system-update";

function readLocalReleases(): ReleaseInfo[] {
  return JSON.parse(process.env.NEXT_PUBLIC_APP_RELEASES || "[]");
}

export function useVersionCheck(isAdmin: boolean) {
  const releases = useMemo(readLocalReleases, []);
  const [state, setState] = useState<SystemUpdate | null>(null);
  const [checking, setChecking] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [connectionError, setConnectionError] = useState("");
  const [open, setOpen] = useState(false);
  const checkingRef = useRef(false);
  const refreshedJob = useRef("");
  const running = isUpdateRunning(state?.job ?? null);

  const checkLatestRelease = useCallback(async (force = false) => {
    if (!isAdmin || checkingRef.current) return;
    checkingRef.current = true;
    setChecking(true);
    try {
      const next = await fetchSystemUpdate(force);
      setState(next);
      setConnectionError("");
      if (force && !next.error) toast.success(next.latest ? "已获取最新发布版本" : "本项目暂未发布正式版本");
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : "版本检查失败");
    } finally {
      checkingRef.current = false;
      setChecking(false);
    }
  }, [isAdmin]);

  useEffect(() => {
    if (!isAdmin) return;
    void checkLatestRelease();
    const timer = window.setInterval(() => { void checkLatestRelease(); }, 300000);
    return () => window.clearInterval(timer);
  }, [isAdmin, checkLatestRelease]);

  useEffect(() => {
    if (!isAdmin || (!open && !running)) return;
    let active = true;
    let pending = false;
    const poll = async () => {
      if (pending) return;
      pending = true;
      try {
        const next = await fetchSystemUpdate(false, true);
        if (active) {
          setState(previous => ({ ...next, latest: next.latest ?? previous?.latest ?? null,
            checked_at: next.checked_at ?? previous?.checked_at ?? null,
            error: next.error ?? previous?.error ?? null,
            has_update: next.latest ? next.has_update : Boolean(previous?.has_update && previous.current_version === next.current_version) }));
          setConnectionError("");
          if (next.job && ["succeeded", "rolled_back"].includes(next.job.state) && refreshedJob.current !== next.job.id) {
            refreshedJob.current = next.job.id;
            void checkLatestRelease();
          }
        }
      } catch (error) {
        if (active) setConnectionError(running ? "服务切换中，正在等待重新连接…" : error instanceof Error ? error.message : "获取更新状态失败");
      } finally { pending = false; }
    };
    const timer = window.setInterval(() => { void poll(); }, running ? 2000 : 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [isAdmin, open, running, checkLatestRelease]);

  const submit = async (operation: "update" | "rollback", version: string) => {
    setSubmitting(true);
    try {
      setState(await submitSystemUpdate(operation, version));
      setConnectionError("");
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : "提交更新任务失败");
    } finally { setSubmitting(false); }
  };

  return { open, setOpen, openReleaseModal: () => { setOpen(true); void checkLatestRelease(); },
    currentVersion: state?.current_version ?? webConfig.appVersion,
    latestVersion: state?.latest?.version ?? "—", releases, checking, submitting, running,
    hasNewVersion: state?.has_update ?? false, checkLatestRelease, state, connectionError, submit };
}
