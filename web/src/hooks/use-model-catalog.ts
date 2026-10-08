"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchModelCatalog, syncModelCatalog, type ModelCatalogResponse } from "@/lib/api";

export function useModelCatalog() {
  const [catalog, setCatalog] = useState<ModelCatalogResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const mounted = useRef(false);
  const pending = useRef<Promise<void> | null>(null);
  const repeat = useRef(false);
  const forceNext = useRef(false);

  const load = useCallback((force = false, recheckAfterCurrent = false): Promise<void> => {
    if (pending.current) {
      // 账号变更可能发生在旧请求期间，完成后再读取一次过滤后的目录。
      if (force || recheckAfterCurrent) repeat.current = true;
      if (force) forceNext.current = true;
      return pending.current;
    }
    if (!mounted.current) return Promise.resolve();
    setLoading(true);
    setSyncing(force);
    const task = (async () => {
      let shouldForce = force;
      do {
        repeat.current = false;
        try {
          const result = await (shouldForce ? syncModelCatalog() : fetchModelCatalog());
          if (mounted.current) {
            setCatalog(result);
            setError(null);
          }
        } catch (cause: unknown) {
          if (mounted.current) {
            // 网络或契约错误保留最近目录；后端有效响应则始终采用其账号状态过滤结果。
            setError(cause instanceof Error ? cause.message : "加载图片目录失败，请稍后重新刷新");
          }
        }
        shouldForce = forceNext.current;
        forceNext.current = false;
        if (shouldForce && mounted.current) setSyncing(true);
      } while (repeat.current && mounted.current);
    })().finally(() => {
      pending.current = null;
      if (mounted.current) {
        setLoading(false);
        setSyncing(false);
      }
    });
    pending.current = task;
    return task;
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    const checkWhenVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    // 仅重新读取本地账号形成的图片目录，不触发上游文本模型同步。
    const interval = window.setInterval(checkWhenVisible, 60_000);
    document.addEventListener("visibilitychange", checkWhenVisible);
    return () => {
      mounted.current = false;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", checkWhenVisible);
    };
  }, [load]);

  const reload = useCallback(() => load(false, true), [load]);
  const sync = useCallback(() => load(true), [load]);

  return { catalog, error, loading, syncing, reload, sync };
}

export type ModelCatalogState = ReturnType<typeof useModelCatalog>;
