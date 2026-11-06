"use client";

import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, LoaderCircle, RefreshCw, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { DateRangeFilter } from "@/components/date-range-filter";
import { ImageLightbox } from "@/components/image-lightbox";
import { LogAccountLink, LogDiagnostics } from "@/components/log-diagnostics";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { deleteSystemLogs, fetchSystemLogs, type SystemLog, type SystemLogFilters } from "@/lib/api";
import { durationText, errorLabels, kindLabels, logChannelLabels, logKind, logLabel, logRecord, logStatus, logText, modelPair, outputCount, safeResultUrls } from "@/lib/log-diagnostics";
import { useAuthGuard } from "@/lib/use-auth-guard";

const initialFilters: SystemLogFilters = { type: "call", start_date: "", end_date: "", status: "", channel: "", task_type: "", model: "", account: "", q: "" };
const pageSize = 20;

function FilterSelect({ label, value, onChange, options }: { label: string; value?: string; onChange: (value: string) => void; options: Record<string, string> }) {
  return <Select value={value || "all"} onValueChange={value => onChange(value === "all" ? "" : value)}><SelectTrigger aria-label={label} className="h-9 w-auto min-w-28 rounded-lg"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">{label}</SelectItem>{Object.entries(options).map(([key, text]) => <SelectItem key={key} value={key}>{text}</SelectItem>)}</SelectContent></Select>;
}

function LogsContent() {
  const [draft, setDraft] = useState<SystemLogFilters>(initialFilters);
  const [filters, setFilters] = useState<SystemLogFilters>(initialFilters);
  const [items, setItems] = useState<SystemLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [deletingItems, setDeletingItems] = useState<SystemLog[]>([]);
  const [deleting, setDeleting] = useState(false);
  const [detailLog, setDetailLog] = useState<SystemLog | null>(null);
  const [lightboxIndex, setLightboxIndex] = useState(0);
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const isCall = filters.type === "call", pageCount = Math.max(1, Math.ceil(total / pageSize));
  const selectedSet = new Set(selectedIds);
  const allSelected = items.length > 0 && items.every(item => selectedSet.has(item.id));

  useEffect(() => {
    let active = true;
    setLoading(true); setLoadError(""); setSelectedIds([]);
    void fetchSystemLogs({ ...filters, page, page_size: pageSize }).then(data => {
      if (!active) return;
      const lastPage = Math.max(1, Math.ceil(data.total / pageSize));
      if (page > lastPage) { setPage(lastPage); return; }
      setItems(data.items); setTotal(data.total);
    }).catch(error => {
      if (active) { setItems([]); setTotal(0); setLoadError(error instanceof Error ? error.message : "加载日志失败"); }
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [filters, page, revision]);

  function applyFilters(next = draft) { setPage(1); setFilters({ ...next }); }
  function updateDraft(name: keyof SystemLogFilters, value: string) { setDraft(current => ({ ...current, [name]: value })); }
  async function confirmDelete() {
    if (!deletingItems.length) return;
    setDeleting(true);
    try {
      const result = await deleteSystemLogs(deletingItems.map(item => item.id));
      toast.success(`已删除 ${result.removed} 条日志`);
      if (deletingItems.some(item => item.id === detailLog?.id)) setDetailLog(null);
      setDeletingItems([]); setSelectedIds([]); setRevision(value => value + 1);
    } catch (error) { toast.error(error instanceof Error ? error.message : "删除日志失败"); }
    finally { setDeleting(false); }
  }

  return <section className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><p className="text-[10px] tracking-[0.18em] text-muted-foreground">REQUEST LOGS</p><h1 className="mt-1 text-xl font-semibold">请求日志</h1></div>
      <div className="flex items-center gap-2"><Select value={filters.type} onValueChange={type => { const next = { ...initialFilters, type }; setDraft(next); applyFilters(next); }}><SelectTrigger aria-label="日志类型" className="h-9 w-36 rounded-xl"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="call">调用日志</SelectItem><SelectItem value="account">账号管理日志</SelectItem></SelectContent></Select><Button variant="outline" className="h-9 gap-2 rounded-xl bg-card shadow-none" onClick={() => setRevision(value => value + 1)} disabled={loading}><RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />刷新</Button></div>
    </div>
    <form className="flex flex-wrap items-center gap-2 rounded-xl border bg-card p-3" onSubmit={event => { event.preventDefault(); applyFilters(); }}>
      <Input aria-label="搜索请求日志" placeholder="请求 ID、任务 ID、摘要" className="h-9 min-w-48 flex-1 basis-56" value={draft.q || ""} onChange={event => updateDraft("q", event.target.value)} />
      <DateRangeFilter startDate={draft.start_date || ""} endDate={draft.end_date || ""} onChange={(start_date, end_date) => setDraft(current => ({ ...current, start_date, end_date }))} />
      {isCall && <><FilterSelect label="全部状态" value={draft.status} onChange={value => updateDraft("status", value)} options={{ failed: "失败", partial: "部分成功", success: "成功" }} /><FilterSelect label="全部渠道" value={draft.channel} onChange={value => updateDraft("channel", value)} options={{ web: "ChatGPT Web", codex: "Codex", grok_web: "Grok Web" }} /><FilterSelect label="全部任务" value={draft.task_type} onChange={value => updateDraft("task_type", value)} options={kindLabels} /><Input aria-label="筛选模型" placeholder="模型名称" className="h-9 w-40" value={draft.model || ""} onChange={event => updateDraft("model", event.target.value)} /><Input aria-label="筛选账号" placeholder="账号邮箱或 ID" className="h-9 w-44" value={draft.account || ""} onChange={event => updateDraft("account", event.target.value)} /></>}
      <Button type="submit" size="sm" className="h-9 rounded-lg" disabled={loading}><Search className="size-4" />查询</Button><Button type="button" variant="ghost" size="sm" onClick={() => { const next = { ...initialFilters, type: filters.type }; setDraft(next); applyFilters(next); }}>重置</Button>
    </form>
    {loadError && <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{loadError}</p>}
    <div className="overflow-hidden rounded-xl border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3 text-xs text-muted-foreground"><span>共 {total} 条 · 每页 {pageSize} 条{selectedIds.length ? ` · 已选 ${selectedIds.length} 条` : ""}</span><Button variant="ghost" size="sm" className="h-7 text-destructive" disabled={!selectedIds.length || loading || deleting} onClick={() => setDeletingItems(items.filter(item => selectedSet.has(item.id)))}><Trash2 className="size-3.5" />删除所选</Button></div>
      <div className="overflow-x-auto" aria-busy={loading}><Table className={isCall ? "min-w-[1050px] [&_th]:whitespace-nowrap [&_td]:whitespace-nowrap" : "min-w-[650px]"}><TableHeader><TableRow><TableHead className="w-10"><Checkbox aria-label="选择本页全部日志" checked={allSelected} disabled={loading} onCheckedChange={value => setSelectedIds(value ? items.map(item => item.id) : [])} /></TableHead><TableHead>时间（UTC+8）</TableHead><TableHead>{isCall ? "任务" : "类型"}</TableHead>{isCall && <><TableHead>模型</TableHead><TableHead>账号 / 渠道</TableHead><TableHead>状态</TableHead><TableHead>耗时</TableHead><TableHead>产出 / 请求</TableHead></>}<TableHead>{isCall ? "摘要 / 错误" : "摘要"}</TableHead><TableHead className="w-28">操作</TableHead></TableRow></TableHeader>
        <TableBody>{loading ? <TableRow><TableCell colSpan={isCall ? 10 : 5} className="h-32 text-center"><LoaderCircle className="mx-auto size-5 animate-spin text-muted-foreground" /><span className="sr-only">加载日志中</span></TableCell></TableRow> : items.length ? items.map(item => {
          const detail = logRecord(item.detail), models = modelPair(detail), status = logStatus(detail);
          return <TableRow key={item.id} className="text-xs"><TableCell><Checkbox aria-label={`选择日志 ${item.id}`} checked={selectedSet.has(item.id)} onCheckedChange={checked => setSelectedIds(current => checked ? [...current, item.id] : current.filter(id => id !== item.id))} /></TableCell><TableCell className="whitespace-nowrap text-muted-foreground">{item.time}</TableCell><TableCell>{isCall ? kindLabels[logKind(detail)] : "账号管理"}</TableCell>{isCall && <><TableCell className="max-w-48"><p className="truncate" title={models.requested}>{models.requested}</p><p className="mt-1 truncate text-[10px] text-muted-foreground" title={models.actual}>实际：{models.actual}</p></TableCell><TableCell className="max-w-48"><LogAccountLink detail={detail} /><p className="mt-1 text-[10px] text-muted-foreground">{logLabel(logChannelLabels, detail.channel)}</p></TableCell><TableCell><Badge variant={status.variant} className="whitespace-nowrap px-2 text-[10px]">{status.label}</Badge></TableCell><TableCell className="whitespace-nowrap tabular-nums">{durationText(detail.duration_ms)}</TableCell><TableCell className="tabular-nums">{outputCount(detail)}</TableCell></>}<TableCell className="max-w-56"><p className="truncate" title={item.summary}>{item.summary || "—"}</p>{!!detail.error && <p className="mt-1 truncate text-[10px] text-destructive" title={logText(detail.error)}>{logLabel(errorLabels, detail.error_category, "") || logText(detail.error_code, "") || logText(detail.error)}</p>}</TableCell><TableCell><div className="flex items-center gap-1"><Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => setDetailLog(item)}>详情</Button><Button variant="ghost" size="icon" className="size-7 text-muted-foreground hover:text-destructive" aria-label={`删除日志 ${item.id}`} onClick={() => setDeletingItems([item])}><Trash2 className="size-3.5" /></Button></div></TableCell></TableRow>;
        }) : <TableRow><TableCell colSpan={isCall ? 10 : 5} className="h-32 text-center text-muted-foreground">没有找到日志</TableCell></TableRow>}</TableBody>
      </Table></div>
      <div className="flex items-center justify-end gap-2 border-t px-4 py-3 text-xs text-muted-foreground"><span>第 {page} / {pageCount} 页</span><Button variant="outline" size="icon" className="size-8 rounded-lg" aria-label="上一页" disabled={loading || page <= 1} onClick={() => setPage(value => value - 1)}><ChevronLeft className="size-4" /></Button><Button variant="outline" size="icon" className="size-8 rounded-lg" aria-label="下一页" disabled={loading || page >= pageCount} onClick={() => setPage(value => value + 1)}><ChevronRight className="size-4" /></Button></div>
    </div>
    <Dialog open={!!detailLog} onOpenChange={open => { if (!open) setDetailLog(null); }}><DialogContent className="flex max-h-[90dvh] w-[calc(100%-2rem)] max-w-4xl flex-col gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-4xl"><DialogHeader className="shrink-0 border-b px-5 py-4"><DialogTitle>日志详情</DialogTitle><DialogDescription>请求参数、素材摘要及执行过程；未采集的历史字段显示未知。</DialogDescription></DialogHeader><div className="min-h-0 overflow-y-auto p-5">{detailLog?.type === "call" ? <LogDiagnostics item={detailLog} onPreview={index => { setLightboxIndex(index); setLightboxOpen(true); }} /> : <pre className="whitespace-pre-wrap break-all text-xs leading-6">{JSON.stringify(detailLog?.detail || {}, null, 2)}</pre>}</div></DialogContent></Dialog>
    <ImageLightbox images={safeResultUrls(logRecord(detailLog?.detail)).map((src, index) => ({ id: String(index), src }))} currentIndex={lightboxIndex} open={lightboxOpen} onOpenChange={setLightboxOpen} onIndexChange={setLightboxIndex} />
    <Dialog open={deletingItems.length > 0} onOpenChange={open => { if (!open && !deleting) setDeletingItems([]); }}><DialogContent><DialogHeader><DialogTitle>删除日志</DialogTitle><DialogDescription>确认删除所选 {deletingItems.length} 条日志？删除后无法恢复。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={deleting} onClick={() => setDeletingItems([])}>取消</Button><Button variant="destructive" disabled={deleting} onClick={() => void confirmDelete()}>{deleting && <LoaderCircle className="size-4 animate-spin" />}确认删除</Button></DialogFooter></DialogContent></Dialog>
  </section>;
}

export default function LogsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || session?.role !== "admin") return <div className="flex min-h-80 items-center justify-center"><LoaderCircle className="size-5 animate-spin text-muted-foreground" /></div>;
  return <LogsContent />;
}
