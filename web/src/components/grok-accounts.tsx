"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { httpRequest } from "@/lib/request";
import { parseGrokAccounts, type GrokAccount } from "@/lib/account-pool";
import type { AccountStatus } from "@/lib/api";
import { AccountActivityBadge, activityRowClass, activityActionClass, mergeActivity } from "./account-activity";
import { cn } from "@/lib/utils";

export async function fetchGrokActivity() {
  return parseGrokAccounts(await httpRequest<unknown>("/api/grok/accounts"));
}

export const grokAccountPath = (id: string) => `/api/grok/accounts/${encodeURIComponent(id)}`;
type Mutation = { path: string; method?: string; body?: unknown };

export function useGrokAccounts() {
  const [accounts, setAccounts] = useState<GrokAccount[]>([]);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const load = useCallback(async () => {
    setAccounts(parseGrokAccounts(await httpRequest<unknown>("/api/grok/accounts")));
  }, []);
  const reload = useCallback(async () => {
    setLoading(true); setError("");
    try { await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "加载 Grok 账号失败"); }
    finally { setLoading(false); }
  }, [load]);
  useEffect(() => { void reload(); }, [reload]);
  const applyActivity = useCallback((snapshot: GrokAccount[]) => {
    if (busyRef.current) return;
    setAccounts(current => current === accounts ? mergeActivity(current, snapshot, item => item.id, "requestInflight") : current);
  }, [accounts]);

  async function mutate(changes: Mutation[]) {
    if (busyRef.current || !changes.length) return false;
    busyRef.current = true; setBusy(true); setError("");
    const failures: string[] = [];
    let succeeded = 0;
    try {
      for (const change of changes) {
        try { await httpRequest<unknown>(change.path, { method: change.method || "POST", body: change.body }); succeeded++; }
        catch (e) { failures.push(e instanceof Error ? e.message : "操作失败"); }
      }
      await load();
      if (failures.length) {
        setError(`Grok：成功 ${succeeded} 项，失败 ${failures.length} 项。${failures[0]}`);
        return false;
      }
      toast.success(`Grok：已完成 ${succeeded} 项操作`);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "刷新 Grok 列表失败，请重新加载");
      return false;
    } finally {
      // 上游认证失败也可能将账号改为 reauth_required，目录必须随之更新。
      setRevision(value => value + 1);
      busyRef.current = false; setBusy(false);
    }
  }
  return { accounts, loading, busy, error, revision, reload, mutate, applyActivity };
}

export function GrokAccountRow({ account: a, status, selected, onSelect, busy, onChange, onDelete }: {
  account: GrokAccount; status: AccountStatus; selected: boolean; onSelect: (checked: boolean) => void;
  busy: boolean; onChange: (change: Mutation) => void; onDelete: () => void;
}) {
  const active = a.requestInflight > 0;
  return <tr className={cn("group border-b border-border/70 text-sm text-muted-foreground", active ? activityRowClass : "hover:bg-secondary/30")}>
    <td className="px-4 py-3"><Checkbox aria-label={`选择 ${a.name}`} checked={selected} onCheckedChange={value => onSelect(value === true)} /></td>
    <td className="max-w-56 px-4 py-3"><p className="truncate font-medium text-foreground" title={a.name}>{a.name}</p><AccountActivityBadge count={a.requestInflight} kind="request" /><p className="mt-1 text-[10px]">SSO 凭据已加密</p></td>
    <td className="px-4 py-3"><Select value={a.tier} disabled={busy} onValueChange={tier => onChange({ path: grokAccountPath(a.id), method: "PATCH", body: { tier } })}><SelectTrigger aria-label={`${a.name}账号等级`} className="h-7 w-28 border-0 bg-secondary/50 px-2 text-xs"><SelectValue /></SelectTrigger><SelectContent>{[["auto", "自动 / Basic"], ["basic", "Basic"], ["super", "Super"], ["heavy", "Heavy"]].map(([v,l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent></Select></td>
    <td className="px-4 py-3"><Badge variant="outline" className="whitespace-nowrap rounded-md">Grok Web</Badge></td>
    <td className="px-3 py-3"><Badge variant={status === "正常" ? "success" : status === "禁用" ? "secondary" : status === "限流" ? "warning" : "danger"} className="whitespace-nowrap rounded-md">{a.enabled && a.status === "reauth_required" ? "需要登录" : status}</Badge></td>
    <td className="px-3 py-3 text-center">—</td><td className="px-3 py-3 text-center">—</td>
    <td className="px-4 py-3 text-xs">{a.email || "—"}</td>
    <td className="whitespace-nowrap px-4 py-3 text-xs">{a.createdAt ? new Date(a.createdAt * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"}</td>
    <td className="min-w-40 px-4 py-3"><div className="flex flex-wrap gap-1">{Object.entries(a.quotas).map(([name, quota]) => <span key={name} className="whitespace-nowrap rounded bg-secondary px-1.5 py-0.5 text-[10px]">{name} {quota.remaining ?? "—"}</span>)}{!Object.keys(a.quotas).length && <span className="whitespace-nowrap text-xs">尚未同步</span>}</div></td>
    <td className="px-4 py-3 text-xs">{status === "限流" ? new Date(a.cooldownUntil * 1000).toLocaleString("zh-CN") : "—"}</td>
    <td className="px-3 py-3 tabular-nums" title="当前在途请求数（含对话、图片和视频）">{a.requestInflight}</td><td className="px-3 py-3">—</td><td className="px-3 py-3">—</td>
    <td className={cn("sticky right-0 z-10 px-4 py-3 before:pointer-events-none before:absolute before:inset-y-0 before:left-0 before:w-px before:bg-border", active ? activityActionClass : "bg-card group-hover:bg-secondary")}><div className="flex items-center gap-1">
      <Button variant="ghost" size="icon" className="size-8" aria-label={`同步 ${a.name}`} disabled={busy} onClick={() => onChange({ path: `${grokAccountPath(a.id)}/sync` })}><RefreshCw className="size-3.5" /></Button>
      <Button variant="ghost" size="sm" className="h-8 px-2 text-xs" disabled={busy} onClick={() => onChange({ path: grokAccountPath(a.id), method: "PATCH", body: { enabled: !a.enabled } })}>{a.enabled ? "禁用" : "启用"}</Button>
      <Button variant="ghost" size="icon" className="size-8 hover:text-destructive" aria-label={`删除 ${a.name}`} disabled={busy} onClick={onDelete}><Trash2 className="size-3.5" /></Button>
    </div></td>
  </tr>;
}
