"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ImageIcon, MessageSquareText, Video } from "lucide-react";
import { cn } from "@/lib/utils";

// Layout adapted from grok2api v3.1.8's creative console (MIT).
export function CreativeTabs() {
  const pathname = usePathname().replace(/\/$/, "");
  return <nav aria-label="创作模式" className="inline-flex shrink-0 items-center gap-1 rounded-full bg-secondary/60 p-1">
    {[{ href: "/chat", label: "对话", icon: MessageSquareText }, { href: "/image", label: "图片", icon: ImageIcon }, { href: "/video", label: "视频", icon: Video }].map(({ href, label, icon: Icon }) =>
      <Link key={href} href={href} aria-current={pathname === href ? "page" : undefined} className={cn("flex h-8 items-center gap-2 rounded-full px-4 text-xs transition-colors", pathname === href ? "bg-background text-foreground shadow-xs" : "text-muted-foreground hover:text-foreground")}><Icon className="size-3.5" />{label}</Link>)}
  </nav>;
}
