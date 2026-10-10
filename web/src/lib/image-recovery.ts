import type { ImageTask } from "./api";

export function isAutomaticImageRecovery(task: ImageTask): boolean {
  return task.status === "uncertain" || task.recovery_active === true ||
    ((task.status === "queued" || task.status === "running") &&
      (task.recovery_status === "auto_pending" || task.recovery_status === "recovering_result"));
}

export function hasImageRecovery(task: ImageTask): boolean {
  return isAutomaticImageRecovery(task) || task.recovery_deadline != null || task.recovery_status === "auto_failed";
}

export function imageRecoveryMessage(task: ImageTask, now = Date.now()): string {
  if (!isAutomaticImageRecovery(task)) {
    if (task.status === "success") return task.partial_success ? "已完成，部分图片未恢复" : "结果已恢复";
    if (task.status === "cancelled") return "任务已结束，预占额度已释放";
    return "自动恢复已结束，预占额度已释放";
  }
  const attempts = `${task.recovery_attempts ?? 0}/${task.recovery_max_attempts ?? 3}`;
  if (task.status === "running" && task.recovery_status === "recovering_result") return `正在自动读取原结果 · 已尝试 ${attempts} 次`;
  if (task.recovery_next_at != null && task.recovery_next_at * 1000 > now) {
    return `自动恢复等待中 · ${Math.ceil((task.recovery_next_at * 1000 - now) / 1000)} 秒后再次读取 · 已尝试 ${attempts} 次`;
  }
  return `等待自动读取原结果 · 已尝试 ${attempts} 次`;
}
