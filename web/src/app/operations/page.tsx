"use client";

import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { fetchRuntimeStatistics, fetchRuntimeTasks } from "@/lib/generation-runtime";
import { hasImageRecovery } from "@/lib/image-recovery";
import { AutoRecoveryTasks } from "@/components/auto-recovery-tasks";
import type { ImageTask } from "@/lib/api";

const metrics = [
  ["requests", "请求数"], ["successful_images", "成功图片"], ["partial_success", "部分成功请求"],
  ["failed", "失败图片任务"], ["cancelled", "取消图片任务"], ["recovering", "自动恢复图片任务"],
  ["platform_failures", "平台故障"], ["invalid_request", "用户参数错误"], ["content_rejected", "内容拒绝"], ["local_rejection", "本地额度 / 容量拒绝"],
];

export default function OperationsPage() {
  const { session, isCheckingAuth } = useAuthGuard(["admin"]);
  const [filters, setFilters] = useState({ days: "1", model: "", channel: "", key_id: "" });
  const [stats, setStats] = useState<Awaited<ReturnType<typeof fetchRuntimeStatistics>> | null>(null);
  const [tasks, setTasks] = useState<ImageTask[]>([]);
  const [error, setError] = useState("");
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (session?.role !== "admin") return;
    let active = true;
    let loading = false;
    const load = async () => {
      if (loading) return;
      loading = true;
      try {
        const [next, jobs] = await Promise.all([fetchRuntimeStatistics(filters), fetchRuntimeTasks()]);
        if (active) { setStats(next); setTasks(jobs.filter(hasImageRecovery)); setNow(Date.now()); setError(""); }
      } catch (error) { if (active) setError(error instanceof Error ? error.message : "加载失败"); }
      finally { loading = false; }
    };
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [session?.role, filters]);
  if (isCheckingAuth || session?.role !== "admin") return <p className="p-8 text-muted-foreground">正在验证权限…</p>;
  const select = (key: keyof typeof filters, label: string, options: Array<[string, string]>) => <div className="space-y-1" key={key}>
    <span className="text-xs text-muted-foreground">{label}</span>
    <Select value={filters[key] || "all"} onValueChange={(value) => setFilters((current) => ({ ...current, [key]: value === "all" ? "" : value }))}>
      <SelectTrigger className="min-w-36"><SelectValue /></SelectTrigger>
      <SelectContent>{options.map(([value, text]) => <SelectItem value={value || "all"} key={value}>{text}</SelectItem>)}</SelectContent>
    </Select>
  </div>;
  const seconds = (value: number | null | undefined) => value == null ? "暂无数据" : `${value.toFixed(1)} 秒`;
  return <div className="space-y-6 py-6">
    <div><h1 className="text-2xl font-semibold">运行统计</h1><p className="mt-2 text-sm text-muted-foreground">每 5 秒刷新。统计从启用时开始，历史日志不计入额度和成功率。</p></div>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <div className="grid gap-3 sm:grid-cols-4">{[["running", "运行中", "global_concurrency"], ["queued", "等待中", "max_waiting_images"], ["cooldown_accounts", "冷却账号", ""], ["recovering", "自动恢复中", ""]].map(([key, label, capacity]) =>
      <Card key={key}><CardContent className="p-5"><p className="text-sm text-muted-foreground">{label}</p><p className="mt-2 text-2xl font-semibold tabular-nums">{stats?.live[key] ?? "—"}{capacity && <span className="text-base text-muted-foreground"> / {stats?.live[capacity] ?? "—"}</span>}</p></CardContent></Card>)}</div>
    <div className="flex flex-wrap gap-3">
      {select("days", "时间", [["1", "最近 24 小时"], ["7", "最近 7 天"], ["30", "最近 30 天"]])}
      {select("model", "模型", [["", "全部模型"], ...(stats?.models ?? []).map((model): [string, string] => [model, model])])}
      {select("channel", "通道", [["", "全部通道"], ["web", "Web"], ["codex", "Codex"]])}
      {select("key_id", "Key", [["", "全部 Key"], ...(stats?.keyIds ?? []).map((key): [string, string] => [key, key])])}
    </div>
    <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">{metrics.map(([key, label]) => <Card key={key}><CardContent className="p-4"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-2 text-xl tabular-nums">{stats?.summary[key] ?? "—"}</p></CardContent></Card>)}</div>
    <Card><CardContent className="grid gap-6 p-6 md:grid-cols-3">
      <div><h2 className="font-medium">平台成功率</h2><p className="mt-2 text-2xl">{stats?.summary.platform_success_rate == null ? "暂无数据" : `${(stats.summary.platform_success_rate * 100).toFixed(1)}%`}</p><p className="mt-2 text-xs text-muted-foreground">成功图片任务 ÷（成功 + 平台故障终态图片任务）；自动恢复中的任务暂不计入</p></div>
      <div><h2 className="font-medium">排队耗时</h2><p className="mt-2">P50 {seconds(stats?.summary.queue_p50)}</p><p>P95 {seconds(stats?.summary.queue_p95)}</p></div>
      <div><h2 className="font-medium">生成及后处理耗时</h2><p className="mt-2">P50 {seconds(stats?.summary.generation_p50)}</p><p>P95 {seconds(stats?.summary.generation_p95)}</p></div>
    </CardContent></Card>
    <Card><CardContent className="space-y-3 p-6"><h2 className="font-medium">Key 当前占用</h2><div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr className="border-b"><th className="p-2">Key ID</th><th>运行 / 上限</th><th>等待</th><th>自动恢复</th></tr></thead><tbody>{stats?.keys.map((key) => <tr key={key.key_id} className="border-b"><td className="p-2 font-mono">{key.key_id}</td><td>{key.values.running} / {key.values.concurrency_limit}</td><td>{key.values.queued}</td><td>{key.values.recovering}</td></tr>)}</tbody></table>{!stats?.keys.length && <p className="py-4 text-sm text-muted-foreground">当前无任务</p>}</div></CardContent></Card>
    <AutoRecoveryTasks tasks={tasks} now={now} />
  </div>;
}
