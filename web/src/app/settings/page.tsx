"use client";

import { useEffect, useRef } from "react";
import { LoaderCircle } from "lucide-react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";

import { BackupSettingsCard } from "./components/backup-settings-card";
import { ApiDocsCard } from "./components/api-docs-card";
import { ConfigCard } from "./components/config-card";
import { ProxySettingsCard } from "./components/proxy-settings-card";
import { ProxyRuntimeCard } from "./components/proxy-runtime-card";
import { SettingsHeader } from "./components/settings-header";
import { SettingsSection } from "./components/settings-section";
import { ImageQueueSettings } from "./components/image-queue-settings";
import { UserKeysCard } from "./components/user-keys-card";
import { useSettingsStore } from "./store";
import { GrokSettings } from "@/components/grok-settings";

const settingsTabs = [
  { value: "basic", title: "号池维护" },
  { value: "proxy", title: "网络代理" },
  { value: "grok", title: "Grok" },
  { value: "api-docs", title: "接口接入" },
  { value: "backup", title: "备份" },
  { value: "advanced", title: "高级设置" },
];

function SettingsDataController() {
  const didLoadRef = useRef(false);
  const initialize = useSettingsStore((state) => state.initialize);
  const loadBackups = useSettingsStore((state) => state.loadBackups);
  const backupState = useSettingsStore((state) => state.backupState);

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void initialize();
  }, [initialize]);

  useEffect(() => {
    if (!backupState?.running) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadBackups(true);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [backupState?.running, loadBackups]);

  return null;
}

function SettingsPageContent() {
  return (
    <>
      <SettingsDataController />
      <SettingsHeader />
      <Tabs defaultValue="basic" className="space-y-4">
        <div className="sticky top-3 z-20 overflow-x-auto rounded-xl border border-white/80 bg-white/90 px-3 py-2 shadow-sm backdrop-blur">
          <TabsList variant="line" className="grid w-full grid-cols-3 sm:flex sm:w-fit sm:justify-start" style={{ height: "auto" }}>
            {settingsTabs.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value} className="h-auto px-4 py-2 group-data-[orientation=horizontal]/tabs:after:bottom-0">
                {tab.title}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        <TabsContent value="basic">
          <ConfigCard />
        </TabsContent>
        <TabsContent value="proxy" className="space-y-4">
          <ProxySettingsCard />
          <SettingsSection title="高级网络设置" description="按需配置 FlareSolverr 与 Cloudflare 验证。">
            <ProxyRuntimeCard />
          </SettingsSection>
        </TabsContent>
        <TabsContent value="grok">
          <GrokSettings />
        </TabsContent>
        <TabsContent value="backup">
          <BackupSettingsCard />
        </TabsContent>
        <TabsContent value="api-docs">
          <ApiDocsCard />
        </TabsContent>
        <TabsContent value="advanced" className="space-y-4">
          <SettingsSection title="图片生成与调度" description="生成参数、图片有效期、分辨率校准与并发队列。">
            <ConfigCard section="images" />
            <ImageQueueSettings />
          </SettingsSection>
          <SettingsSection title="图片存储" description="按需使用 R2 / S3 或 WebDAV 保存图片。">
            <ConfigCard section="storage" />
          </SettingsSection>
          <SettingsSection title="请求与日志" description="日志级别、附加指令、敏感词与 AI 审核。">
            <ConfigCard section="requests" />
          </SettingsSection>
          <SettingsSection title="调用密钥" description="需要隔离不同客户端的调用额度时，创建独立密钥。">
            <UserKeysCard />
          </SettingsSection>
        </TabsContent>
      </Tabs>
    </>
  );
}

export default function SettingsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <SettingsPageContent />;
}
