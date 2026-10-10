import type { ImageTask } from "@/lib/api";
import { imageRecoveryMessage, isAutomaticImageRecovery } from "@/lib/image-recovery";
import { Card, CardContent } from "@/components/ui/card";

export function AutoRecoveryTasks({ tasks, now }: { tasks: ImageTask[]; now: number }) {
  const visible = [...tasks].sort((a, b) => Number(isAutomaticImageRecovery(b)) - Number(isAutomaticImageRecovery(a)) ||
    b.updated_at.localeCompare(a.updated_at)).slice(0, 20);
  return <Card><CardContent className="space-y-3 p-6">
    <h2 className="font-medium">自动结果恢复</h2>
    <p className="text-xs text-muted-foreground">系统自动读取原结果，最多尝试 3 次，恢复窗口 10 分钟。成功后交付图片；无法恢复时自动结束并释放本地预占额度。不会重新生图，无需手动确认。</p>
    {visible.map((task) => <div className="space-y-2 border-t py-3 text-sm" key={task.task_id || task.id}>
      <p className="break-all">{task.model} · {task.task_id || task.id}</p>
      <p className={task.status === "error" ? "text-destructive" : "text-muted-foreground"}>{imageRecoveryMessage(task, now)}</p>
      {task.error && <p className="text-xs text-muted-foreground">{task.error}</p>}
      {isAutomaticImageRecovery(task) && task.recovery_deadline != null && <p className="text-xs text-muted-foreground">剩余恢复时间：{Math.max(0, Math.ceil((task.recovery_deadline * 1000 - now) / 60_000))} 分钟</p>}
      {!!task.data?.length && <div className="flex flex-wrap gap-2">{task.data.map((image, index) => image.url && /^(https?:\/\/|\/)/.test(image.url) ?
        <a key={`${image.url}-${index}`} href={image.url} target="_blank" rel="noreferrer" className="space-y-1 rounded-lg border p-2">
          {/* Generated images use their original storage URL. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={image.url} alt={`恢复结果 ${index + 1}`} className="size-24 rounded object-contain" loading="lazy" />
          <span className="block text-center text-xs">查看图片 {index + 1}</span>
        </a> : null)}</div>}
    </div>)}
    {!visible.length && <p className="text-sm text-muted-foreground">暂无自动恢复记录</p>}
    {!!visible.length && <p className="text-xs text-muted-foreground">展示全局最近 20 条恢复任务，处理中的任务优先。</p>}
  </CardContent></Card>;
}
