"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { getCreativeRoutes } from "@/components/creative-navigation";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

// Layout adapted from grok2api v3.1.8's creative console (MIT).
export function CreativeTabs() {
  const pathname = usePathname().replace(/\/$/, "");
  const { session } = useAuthGuard();
  return <nav aria-label="创作模式" className="inline-flex min-w-0 max-w-full items-center gap-1 overflow-x-auto rounded-full bg-secondary/60 p-1">
    {getCreativeRoutes(session?.role).map(({ href, tabLabel, icon: Icon }) =>
      <Link key={href} href={href} aria-current={pathname === href ? "page" : undefined} className={cn("flex h-8 shrink-0 items-center gap-2 whitespace-nowrap rounded-full px-4 text-xs transition-colors", pathname === href ? "bg-background text-foreground shadow-xs" : "text-muted-foreground hover:text-foreground")}><Icon className="size-3.5" />{tabLabel}</Link>)}
  </nav>;
}
