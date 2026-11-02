"use client";
/* eslint-disable @next/next/no-img-element -- User-provided base64 previews stay in the browser. */
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, Check, Copy, History as HistoryIcon, LoaderCircle, MessageSquareText, Paperclip, Plus, Square, Trash2, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { CreativeTabs } from "@/components/creative-tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { httpRequest } from "@/lib/request";
import { chatStream, imageData, items, record } from "@/lib/native-media";

type Message = { role: "user" | "assistant"; content: string; images?: string[] };
type History = { id: string; title: string; model: string; messages: Message[] };
function history(value: Record<string, unknown>): History | null {
  if (typeof value.id !== "string" || typeof value.model !== "string" || !Array.isArray(value.messages)) return null;
  const messages: Message[] = [];
  for (const m of value.messages) {
    const v = record(m);
    if ((v.role === "user" || v.role === "assistant") && typeof v.content === "string") messages.push({ role: v.role, content: v.content });
  }
  return { id: value.id, title: String(value.title || "对话"), model: value.model, messages };
}
function Chat() {
  const [models, setModels] = useState<string[]>(["auto"]);
  const [model, setModel] = useState("auto");
  const [messages, setMessages] = useState<Message[]>([]);
  const [histories, setHistories] = useState<History[]>([]);
  const [id, setId] = useState(() => crypto.randomUUID());
  const [prompt, setPrompt] = useState("");
  const [images, setImages] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [copied, setCopied] = useState<number | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => { if (follow.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight; }, [messages, busy]);
  const reload = useCallback(async () => {
    const response = await httpRequest<unknown>("/api/conversations");
    setHistories(items(response).map(history).filter((h): h is History => h !== null));
  }, []);
  useEffect(() => {
    void reload().catch(e => setError(String(e)));
    httpRequest<unknown>("/v1/models").then(v => setModels(["auto", ...items(v, "data").filter(m => {
      const capabilities = m.capabilities;
      return Array.isArray(capabilities) ? capabilities.includes("chat") : !String(m.id).includes("image");
    }).map(m => String(m.id)).filter(m => m !== "auto")])).catch(e => setError(String(e)));
    return () => controller.current?.abort();
  }, [reload]);
  async function send() {
    if (controller.current || busy || (!prompt.trim() && !images.length)) return;
    follow.current = true;
    const next: Message[] = [...messages, { role: "user", content: prompt, images }, { role: "assistant", content: "" }];
    const active = new AbortController(); controller.current = active;
    setMessages(next); setPrompt(""); setImages([]); setBusy(true); setError("");
    try {
      const body = { model, stream: true, messages: next.slice(0,-1).map(m => ({ role: m.role, content: m.images?.length
        ? [{ type: "text", text: m.content }, ...m.images.map(url => ({ type: "image_url", image_url: { url } }))] : m.content })) };
      for await (const text of chatStream(body, active.signal)) {
        next[next.length-1] = { ...next[next.length-1], content: next[next.length-1].content + text };
        setMessages([...next]);
      }
    } catch (e) { if (!active.signal.aborted) setError(e instanceof Error ? e.message : "对话失败"); }
    finally {
      controller.current = null;
      try {
        await httpRequest(`/api/conversations/${id}`, { method: "PUT", body: { title: next[0].content.slice(0,40) || "图片对话", model,
          messages: next.map(m => ({ role: m.role, content: m.content })) } });
        await reload();
      } catch (e) { setError(e instanceof Error ? e.message : "保存历史失败"); }
      setBusy(false);
    }
  }
  function newChat() { setId(crypto.randomUUID()); setMessages([]); setImages([]); setPrompt(""); setError(""); follow.current = true; }
  return <div className="creative-workspace flex min-h-0 flex-col">
    <div className="flex shrink-0 items-center justify-between gap-2 max-[360px]:flex-wrap"><CreativeTabs /><div className="flex items-center gap-1">
      <Button variant="ghost" size="icon" className="rounded-full" title="新建对话" aria-label="新建对话" disabled={busy} onClick={newChat}><Plus /></Button>
      <Sheet open={historyOpen} onOpenChange={setHistoryOpen}><SheetTrigger asChild><Button variant="ghost" size="icon" className="rounded-full" aria-label="对话历史" title="对话历史" disabled={busy}><HistoryIcon /></Button></SheetTrigger>
        <SheetContent className="flex flex-col"><SheetHeader><SheetTitle>对话历史</SheetTitle><SheetDescription>保存文字消息，图片附件仅用于当前对话。</SheetDescription></SheetHeader>
          <div className="flex-1 space-y-1 overflow-y-auto px-4 pb-6">{!histories.length && <p className="py-12 text-center text-xs text-muted-foreground">还没有历史对话</p>}
            {histories.map(h => <div key={h.id} className={`group flex items-center gap-2 rounded-lg p-2 ${h.id === id ? "bg-secondary" : "hover:bg-secondary/50"}`}>
              <button className="min-w-0 flex-1 text-left" onClick={() => { setId(h.id); setModel(h.model); setMessages(h.messages); setImages([]); setPrompt(""); setError(""); setHistoryOpen(false); follow.current = true; }}><p className="truncate text-sm">{h.title}</p><p className="mt-1 text-[10px] text-muted-foreground">{h.model}</p></button>
              <Button variant="ghost" size="icon" className="size-8 text-muted-foreground" aria-label={`删除 ${h.title}`} onClick={() => void httpRequest(`/api/conversations/${h.id}`, { method: "DELETE" }).then(async () => { if (h.id === id) newChat(); await reload(); }).catch(e => setError(String(e)))}><Trash2 className="size-3.5" /></Button>
            </div>)}
          </div>
        </SheetContent></Sheet>
    </div></div>
    <div ref={scroller} onScroll={e => { const el = e.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100; }} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
      {!messages.length ? <div className="mx-auto flex min-h-full max-w-2xl flex-col items-center justify-center px-4 py-12 text-center">
        <MessageSquareText className="mb-5 size-8 text-muted-foreground/60" strokeWidth={1.2} /><h1 className="text-2xl font-medium tracking-tight sm:text-3xl">有什么想法，一起聊聊</h1><p className="mt-3 text-sm text-muted-foreground">从一个问题开始，让灵感继续生长。</p>
        <div className="mt-8 flex flex-wrap justify-center gap-2">{["帮我梳理一个新想法", "把复杂的概念讲清楚", "一起写点有趣的内容"].map(text => <button key={text} className="rounded-full border border-border/70 px-4 py-2 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground" onClick={() => setPrompt(text)}>{text}</button>)}</div>
      </div> : <div className="mx-auto max-w-3xl space-y-7 px-2 py-8 sm:px-6" aria-live="polite" aria-busy={busy}>{messages.map((m,i) => <article key={i} className={m.role === "user" ? "ml-auto max-w-[85%]" : "group"}>
        {m.role === "user" ? <div className="rounded-2xl bg-secondary/70 px-5 py-3"><p className="whitespace-pre-wrap break-words text-sm">{m.content}</p>{m.images?.map((src,index) => <img key={index} src={src} alt={`上传图片 ${index+1}`} className="mt-3 max-h-48 rounded-lg object-contain" />)}</div> : <><p className="mb-2 text-[11px] text-muted-foreground">{model}</p><div className="markdown-message text-sm">{m.content ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown> : <span className="flex items-center gap-2 text-muted-foreground">{busy && <LoaderCircle className="size-3 animate-spin" />}{busy ? "正在思考" : "回复已停止"}</span>}</div>
          {m.content && !busy && <Button variant="ghost" size="icon" className="mt-2 size-7 text-muted-foreground" aria-label="复制回复" title="复制回复" onClick={async () => { try { await navigator.clipboard.writeText(m.content); setCopied(i); setTimeout(() => setCopied(null), 1500); } catch { toast.error("复制失败，请手动选择文字"); } }}>{copied === i ? <Check className="size-3" /> : <Copy className="size-3" />}</Button>}</>}
      </article>)}</div>}
    </div>
    <div className="mx-auto w-full max-w-3xl shrink-0 pb-2 pt-3 sm:px-4">
      {error && <p role="alert" className="mb-3 rounded-lg bg-destructive/5 px-3 py-2 text-xs text-destructive">{error}</p>}
      <form className="composer" onSubmit={e => { e.preventDefault(); void send(); }}>
        {images.length > 0 && <div className="flex gap-2 overflow-x-auto px-4 pt-3">{images.map((src,i) => <div key={i} className="relative shrink-0"><img src={src} alt={`待发送图片 ${i+1}`} className="size-14 rounded-lg object-cover" /><button type="button" aria-label={`移除图片 ${i+1}`} className="absolute -right-1 -top-1 rounded-full border bg-background p-0.5" onClick={() => setImages(images.filter((_,index) => index !== i))}><X className="size-3" /></button></div>)}</div>}
        <Textarea aria-label="对话内容" value={prompt} disabled={busy} onChange={e => setPrompt(e.target.value)} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} placeholder="发送消息，或添加一张图片…" className="min-h-24 max-h-48 resize-y rounded-none border-0 bg-transparent px-4 py-4 text-sm shadow-none focus-visible:ring-0" />
        <div className="flex items-center justify-between gap-2 px-3 pb-3"><div className="flex min-w-0 items-center gap-1">
          <input ref={fileInput} aria-label="上传对话图片" className="hidden" type="file" accept="image/*" multiple disabled={busy} onChange={e => { const files = Array.from(e.target.files || []).slice(0,8-images.length); e.target.value = ""; void Promise.all(files.map(imageData)).then(values => setImages(prev => [...prev,...values].slice(0,8))).catch(e => setError(String(e))); }} />
          <Button type="button" variant="ghost" size="icon" className="size-8 rounded-full text-muted-foreground" aria-label="添加图片" title="添加图片" disabled={busy || images.length >= 8} onClick={() => fileInput.current?.click()}><Paperclip className="size-4" /></Button>
          <Select value={model} disabled={busy} onValueChange={setModel}><SelectTrigger aria-label="对话模型" className="h-8 w-auto max-w-48 gap-2 rounded-md border-0 bg-transparent px-2 text-xs shadow-none"><SelectValue /></SelectTrigger><SelectContent>{[...new Set([model,...models])].map(m => <SelectItem key={m} value={m}>{m}</SelectItem>)}</SelectContent></Select>
        </div>{busy ? <Button type="button" size="icon" className="size-8 rounded-full" aria-label="停止生成" title="停止生成" onClick={() => controller.current?.abort()}><Square className="size-3 fill-current" /></Button> : <Button type="submit" size="icon" className="size-8 rounded-full" aria-label="发送" title="发送" disabled={!prompt.trim() && !images.length}><ArrowUp className="size-4" /></Button>}</div>
      </form><p className="mt-2 text-center text-[10px] text-muted-foreground/70">Enter 发送 · Shift + Enter 换行 · AI 生成内容请注意核实</p>
    </div>
  </div>;
}
export default function ChatPage() {
  const { session, isCheckingAuth } = useAuthGuard();
  return isCheckingAuth || !session ? <p className="p-8 text-stone-500">正在验证登录…</p> : <Chat />;
}
