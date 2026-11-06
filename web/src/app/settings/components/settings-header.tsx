"use client";

export function SettingsHeader() {
  return (
    <section className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
      <div className="space-y-1">
        <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">Settings</div>
        <h1 className="text-2xl font-semibold tracking-tight">设置</h1>
        <p className="text-sm text-stone-500">维护自用号池、连接代理与 API 客户端。</p>
      </div>
    </section>
  );
}
