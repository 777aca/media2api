"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import { LoaderCircle } from "lucide-react";

export const activityRowClass = "bg-blue-50 hover:bg-blue-100 dark:bg-blue-950 dark:hover:bg-blue-900";
export const activityActionClass = "bg-blue-50 group-hover:bg-blue-100 dark:bg-blue-950 dark:group-hover:bg-blue-900";

export function AccountActivityBadge({ count, kind = "image" }: { count: number; kind?: "image" | "request" }) {
  if (!(count > 0)) return null;
  return <span role="status" className="mt-1 inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border border-blue-300 bg-blue-100 px-2 py-1 text-xs font-semibold text-blue-800 dark:border-blue-700 dark:bg-blue-900 dark:text-blue-100">
    <LoaderCircle aria-hidden="true" className="size-3.5 motion-safe:animate-spin" />
    {kind === "image" ? `生成中 · ${count} 张` : `处理中 · ${count} 个请求`}
  </span>;
}

// Merge only activity: a local read must not undo edits, restore deleted rows or replace tokens.
export function mergeActivity<T, K extends keyof T>(current: T[], snapshot: T[], key: (item: T) => string, field: K): T[] {
  const counts = new Map(snapshot.map(item => [key(item), item[field]]));
  let changed = false;
  const next = current.map(item => {
    const count = counts.get(key(item));
    if (typeof count !== "number" || !Number.isSafeInteger(count) || count < 0 || item[field] === count) return item;
    changed = true;
    return { ...item, [field]: count };
  });
  return changed ? next : current;
}

export function createActivityPoller<T>({ fetch, canPoll, apply }: {
  fetch: () => Promise<T>; canPoll: () => boolean; apply: (value: T) => void;
}) {
  let pending = false;
  let generation = 0;
  return {
    invalidate: () => { generation++; },
    poll: async () => {
      if (pending || !canPoll()) return;
      pending = true;
      const started = generation;
      try {
        const value = await fetch();
        if (started === generation && canPoll()) apply(value);
      } catch {
        // A failed background read leaves the last activity visible; retry on the next tick.
      } finally {
        pending = false;
      }
    },
  };
}

export function useActivityPolling<T>(options: {
  fetch: () => Promise<T>; apply: (value: T) => void; paused: boolean;
}) {
  const latest = useRef(options);
  const pollerRef = useRef<ReturnType<typeof createActivityPoller<T>> | null>(null);
  const { fetch, apply, paused } = options;
  useLayoutEffect(() => {
    latest.current = { fetch, apply, paused };
    pollerRef.current?.invalidate();
  }, [fetch, apply, paused]);
  useEffect(() => {
    const poller = createActivityPoller({
      fetch: () => latest.current.fetch(),
      canPoll: () => !latest.current.paused && document.visibilityState === "visible",
      apply: (value: T) => latest.current.apply(value),
    });
    pollerRef.current = poller;
    const timer = window.setInterval(() => { void poller.poll(); }, 5000);
    const onVisibility = () => {
      poller.invalidate();
      if (document.visibilityState === "visible") void poller.poll();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      poller.invalidate();
      pollerRef.current = null;
    };
  }, []);
}
