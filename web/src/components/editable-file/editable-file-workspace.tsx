"use client";

import { LoaderCircle } from "lucide-react";
import { CreativeTabs } from "@/components/creative-tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { EditableFilePanel } from "./editable-file-panel";

const modes = {
  ppt: {
    title: "PPT 生成",
    endpoint: "/v1/ppt/generations",
    defaultPrompt: "需要制作一份《2026年Q2 电商运营工作汇报》PPT，用于公司管理层季度会议汇报，整体页数控制在 8 页以内，风格偏商务科技感。重点体现销售增长、用户增长、广告投放效果以及618活动成果，并通过折线图、柱状图、环形图、漏斗图呈现。",
    imageRequired: false,
  },
  psd: {
    title: "PSD 生成",
    endpoint: "/v1/psd/generations",
    defaultPrompt: "按原图位置拆分海报元素并合成可编辑 PSD，保留背景和每个元素图层位置，同时输出每个图层素材 zip。",
    imageRequired: true,
  },
};

export function EditableFileWorkspace({ kind }: { kind: keyof typeof modes }) {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || session?.role !== "admin") {
    return <div className="flex min-h-80 items-center justify-center"><LoaderCircle aria-label="正在验证登录" className="size-5 animate-spin text-muted-foreground" /></div>;
  }
  return <div className="flex min-w-0 flex-col gap-4 xl:h-[calc(100dvh-104px)] xl:min-h-[560px]">
    <div className="min-w-0 shrink-0"><CreativeTabs /></div>
    <EditableFilePanel key={kind} kind={kind} {...modes[kind]} />
  </div>;
}
