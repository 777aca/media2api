"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { Box, ChevronRight, Images, LogOut, Menu, ScrollText, Settings, Users } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { HeaderActions } from "@/components/header-actions";
import { creativeRoutes, getCreativeRoutes } from "@/components/creative-navigation";
import { Sheet, SheetClose, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { getValidatedAuthSession } from "@/lib/auth-session";
import { cn } from "@/lib/utils";
import { clearStoredAuthSession, type StoredAuthSession } from "@/store/auth";

const management = [
  { href: "/accounts", label: "账号管理", icon: Users },
  { href: "/image-manager", label: "图片画廊", icon: Images },
  { href: "/logs", label: "请求日志", icon: ScrollText },
  { href: "/settings", label: "系统设置", icon: Settings },
];

/** Sidebar proportions and navigation treatment adapted from grok2api (MIT). */
export function TopNav({ children }: { children: ReactNode }) {
  const path = usePathname().replace(/\/$/, "") || "/";
  const router = useRouter();
  const [session, setSession] = useState<StoredAuthSession | null>(null);
  useEffect(() => {
    let active = true;
    if (path !== "/login") void getValidatedAuthSession().then(value => { if (active) setSession(value); });
    return () => { active = false; };
  }, [path]);
  if (path === "/login") return <main className="min-h-dvh">{children}</main>;
  const admin = session?.role === "admin";
  const creative = getCreativeRoutes(session?.role);
  const title = [...creativeRoutes, ...management].find(item => item.href === path)?.label || "工作台";
  const links = (mobile = false) => <nav aria-label={mobile ? "移动导航" : "主导航"} className="flex-1 space-y-7 overflow-y-auto py-7">
    {[{ title: "创作空间", items: creative }, ...(admin ? [{ title: "管理", items: management }] : [])].map(group => <div key={group.title} className="space-y-1">
      <p className="mb-3 px-3 text-[11px] text-muted-foreground/70">{group.title}</p>
      {group.items.map(({ href, label, icon: Icon }) => {
        const link = <Link href={href} aria-current={path === href ? "page" : undefined} className={cn("flex h-9 items-center gap-3 rounded-md px-3 text-[13px] transition-colors", path === href ? "bg-secondary/70 text-foreground" : "text-muted-foreground hover:bg-secondary/50 hover:text-foreground")}><Icon className="size-4" strokeWidth={1.7} />{label}</Link>;
        return mobile ? <SheetClose asChild key={href}>{link}</SheetClose> : <div key={href}>{link}</div>;
      })}
    </div>)}
  </nav>;
  const brand = <Link href="/chat" className="flex items-center gap-2.5 text-base font-semibold tracking-tight"><Box className="size-5" strokeWidth={1.6} />media2api<span className="rounded border px-1.5 py-0.5 text-[9px] font-normal tracking-normal text-muted-foreground">WORKSPACE</span></Link>;
  const account = <div className="shrink-0 space-y-4 border-t pt-4">
    <HeaderActions showGithubText={false} isAdmin={admin} />
    <div className="flex items-center gap-2.5"><div className="flex size-8 items-center justify-center rounded-full bg-secondary text-xs font-medium">{(session?.name || "U").slice(0, 1)}</div>
      <div className="min-w-0 flex-1"><p className="truncate text-xs">{session?.name || "正在加载"}</p><p className="mt-0.5 text-[10px] text-muted-foreground">{admin ? "管理员" : "个人工作台"}</p></div>
      <button aria-label="退出登录" title="退出登录" className="rounded-md p-2 text-muted-foreground hover:bg-secondary hover:text-foreground" onClick={async () => { await clearStoredAuthSession(); router.replace("/login"); }}><LogOut className="size-4" /></button>
    </div>
  </div>;
  return <div className="min-h-dvh bg-background">
    <a href="#workspace-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:bg-background focus:p-3">跳到主内容</a>
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 flex-col border-r border-sidebar-border/70 bg-sidebar px-5 py-6 lg:flex">{brand}{links()}{account}</aside>
    <div className="lg:ml-64">
      <header className="flex h-14 items-center gap-3 border-b border-border/60 px-4 lg:px-8">
        <Sheet><SheetTrigger className="rounded-md p-2 text-muted-foreground hover:bg-secondary lg:hidden" aria-label="打开导航"><Menu className="size-4" /></SheetTrigger>
          <SheetContent side="left" className="flex w-72 flex-col bg-sidebar p-5"><SheetHeader className="p-0"><SheetTitle>media2api</SheetTitle></SheetHeader>{links(true)}{account}</SheetContent></Sheet>
        <span className="hidden text-xs text-muted-foreground sm:inline">工作台</span><ChevronRight className="hidden size-3 text-muted-foreground/50 sm:block" /><span className="text-xs">{title}</span>
        <span className="ml-auto flex items-center gap-1.5 text-[10px] text-muted-foreground"><span className="size-1.5 rounded-full bg-emerald-500/80" />{creative.some(item => item.href === path) ? "创作空间" : "管理控制台"}</span>
      </header>
      <main id="workspace-content" className="workspace-content mx-auto flex min-w-0 max-w-[1600px] flex-col gap-5 p-4 lg:px-8 lg:py-6">{children}</main>
    </div>
  </div>;
}
