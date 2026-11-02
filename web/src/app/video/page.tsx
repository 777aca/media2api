"use client";
import { useCallback, useEffect, useState } from "react";
import { ArrowUp, Download, Film, LoaderCircle, Play, RotateCcw, Video as VideoIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { CreativeTabs } from "@/components/creative-tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { httpRequest } from "@/lib/request";
import { apiUrl, items, record } from "@/lib/native-media";

function Video() {
  const [jobs, setJobs] = useState<Record<string, unknown>[]>([]);
  const [available, setAvailable] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [duration, setDuration] = useState(6);
  const [ratio, setRatio] = useState("16:9");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [playback, setPlayback] = useState<Record<string,string>>({});
  const reload = useCallback(async () => { setJobs(items(await httpRequest<unknown>("/api/video-tasks"))); }, []);
  useEffect(() => {
    void reload().catch(e => setError(String(e)));
    void httpRequest<unknown>("/api/video-models").then(v => setAvailable(items(v,"data").length > 0)).catch(e => setError(String(e)));
    const timer = window.setInterval(() => void reload().catch(e => setError(String(e))), 4000);
    return () => clearInterval(timer);
  }, [reload]);
  async function submit() {
    setBusy(true); setError("");
    try { await httpRequest("/api/video-tasks", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
      body: { model: "grok-imagine-video", prompt, duration, aspect_ratio: ratio, resolution: "720p" } }); setPrompt(""); await reload(); }
    catch (e) { setError(e instanceof Error ? e.message : "提交失败"); }
    finally { setBusy(false); }
  }
  async function play(id: string) {
    try {
      const value = record(await httpRequest<unknown>(`/api/video-tasks/${id}/playback`, { method: "POST" }));
      if (typeof value.path !== "string" || !value.path.startsWith("/v1/videos/")) throw new Error("播放链接无效");
      setPlayback(p => ({ ...p, [id]: apiUrl(value.path as string) }));
    } catch (e) { setError(e instanceof Error ? e.message : "播放失败"); }
  }
  const status: Record<string,string> = { queued: "排队中", in_progress: "生成中", completed: "已完成", failed: "失败", uncertain: "结果待确认" };
  return <div className="creative-workspace flex min-h-0 flex-col">
    <div className="flex shrink-0 items-center justify-between gap-3"><CreativeTabs /><span className="text-[11px] text-muted-foreground">{jobs.length ? `${jobs.length} 个创作` : "文生视频"}</span></div>
    <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain py-6">
      {!jobs.length ? <div className="flex min-h-full flex-col items-center justify-center px-4 py-10 text-center"><Film className="mb-5 size-8 text-muted-foreground/60" strokeWidth={1.2} /><h1 className="text-2xl font-medium tracking-tight sm:text-3xl">让想象，动起来</h1><p className="mt-3 max-w-sm text-sm leading-6 text-muted-foreground">描述一个场景、一段运动或一次镜头推进，<br className="hidden sm:block" />开始你的第一个视频创作。</p>
        <div className="mt-8 flex flex-wrap justify-center gap-2">{["晨雾中的森林，镜头缓缓前移", "雨夜街头，霓虹倒映在水面"].map(text => <button key={text} className="rounded-full border border-border/70 px-4 py-2 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground" onClick={() => setPrompt(text)}>{text}</button>)}</div>
      </div> : <section aria-label="视频创作记录" className="mx-auto grid max-w-5xl gap-6 sm:grid-cols-2">{jobs.map(j => <article key={String(j.id)} className="min-w-0 overflow-hidden rounded-xl border border-border/70">
        <div className="relative flex aspect-video items-center justify-center bg-secondary/50">
          {playback[String(j.id)] ? <video className="h-full w-full object-contain" controls preload="metadata" src={playback[String(j.id)]} /> : j.status === "completed" ? <Button variant="secondary" size="icon" className="size-12 rounded-full" aria-label="播放视频" onClick={() => void play(String(j.id))}><Play className="size-5" /></Button> : <div className="flex flex-col items-center gap-3 text-muted-foreground">{["queued", "in_progress"].includes(String(j.status)) ? <LoaderCircle className="size-6 animate-spin" strokeWidth={1.3} /> : <VideoIcon className="size-7" strokeWidth={1.2} />}<span className="text-xs">{status[String(j.status)] || String(j.status)}{j.status === "in_progress" ? ` · ${Number(j.progress)}%` : ""}</span></div>}
          {j.status === "in_progress" && <div role="progressbar" aria-label="视频生成进度" aria-valuenow={Number(j.progress)} aria-valuemin={0} aria-valuemax={100} className="absolute bottom-0 left-0 h-0.5 bg-foreground/40 transition-all" style={{ width: `${Math.max(0,Math.min(100,Number(j.progress)))}%` }} />}
        </div><div className="space-y-3 p-4"><p className="line-clamp-3 text-sm leading-6">{String(j.prompt)}</p><div className="flex items-center justify-between text-[11px] text-muted-foreground"><span>{Number(j.actual_duration || j.duration)} 秒 · {String(j.aspect_ratio || "16:9")} · 720p</span><span>{status[String(j.status)]}</span></div>
          {j.error ? <p className="text-xs leading-5 text-destructive">{String(j.error)}</p> : null}
          <div className="flex flex-wrap gap-1"><Button variant="ghost" size="sm" className="h-7 px-2 text-xs text-muted-foreground" onClick={() => { setPrompt(String(j.prompt)); setDuration(Number(j.duration) || 6); setRatio(String(j.aspect_ratio || "16:9")); }}><RotateCcw className="size-3" />复用提示词</Button>
            {j.status === "uncertain" && <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => void httpRequest(`/api/video-tasks/${j.id}/retry-download`, { method: "POST" }).then(reload).catch(e => setError(String(e)))}>重试下载结果</Button>}
            {j.status === "completed" && <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => void play(String(j.id))}>刷新播放链接</Button>}
            {playback[String(j.id)] && <a className="ml-auto inline-flex items-center gap-1.5 text-xs" href={playback[String(j.id)]} download={`${String(j.id)}.mp4`}><Download className="size-3" />下载</a>}
          </div>
        </div>
      </article>)}</section>}
    </div>
    <div className="mx-auto w-full max-w-3xl shrink-0 pb-2 pt-3 sm:px-4">
      {!available && <p className="mb-3 rounded-lg bg-secondary/60 px-3 py-2 text-xs text-muted-foreground">暂无可用视频模型，请管理员在账号管理中导入 Grok Web 账号。</p>}
      {error && <p role="alert" className="mb-3 rounded-lg bg-destructive/5 px-3 py-2 text-xs text-destructive">{error}</p>}
      <form className="composer" onSubmit={e => { e.preventDefault(); if (!busy && available && prompt.trim()) void submit(); }}>
        <Textarea aria-label="视频提示词" disabled={busy} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="描述场景、主体运动和镜头…" className="min-h-24 max-h-48 resize-y rounded-none border-0 bg-transparent px-4 py-4 text-sm shadow-none focus-visible:ring-0" />
        <div className="flex flex-wrap items-center justify-between gap-2 px-3 pb-3"><div className="flex flex-wrap items-center gap-1 text-xs">
          <span className="hidden px-2 text-muted-foreground sm:inline">Grok Imagine</span>
          <Select value={String(duration)} onValueChange={v => setDuration(Number(v))} disabled={busy}><SelectTrigger aria-label="视频时长" className="h-8 w-20 rounded-md border-0 bg-transparent px-2 text-xs shadow-none"><SelectValue /></SelectTrigger><SelectContent>{Array.from({length:15},(_,i) => i+1).map(n => <SelectItem key={n} value={String(n)}>{n} 秒</SelectItem>)}</SelectContent></Select>
          <Select value={ratio} onValueChange={setRatio} disabled={busy}><SelectTrigger aria-label="视频比例" className="h-8 w-20 rounded-md border-0 bg-transparent px-2 text-xs shadow-none"><SelectValue /></SelectTrigger><SelectContent>{["16:9","9:16","1:1","4:3","3:4","3:2","2:3"].map(r => <SelectItem key={r} value={r}>{r}</SelectItem>)}</SelectContent></Select>
          <span className="px-2 text-muted-foreground">720p</span>
        </div><Button type="submit" size="icon" className="size-8 rounded-full" aria-label="生成视频" title="生成视频" disabled={busy || !available || !prompt.trim()}>{busy ? <LoaderCircle className="size-4 animate-spin" /> : <ArrowUp className="size-4" />}</Button></div>
      </form><p className="mt-2 text-center text-[10px] text-muted-foreground/70">生成任务可在后台继续 · Basic 账号实际时长受账号上限限制</p>
    </div>
  </div>;
}
export default function VideoPage() {
  const { session, isCheckingAuth } = useAuthGuard();
  return isCheckingAuth || !session ? <p className="p-8 text-stone-500">正在验证登录…</p> : <Video />;
}
