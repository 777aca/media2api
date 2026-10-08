"use client";

import { useCallback, useMemo, useState } from "react";
import { toast } from "sonner";

import webConfig from "@/constants/common-env";
import { GITHUB_RAW_URL } from "@/constants/project";
import { parseProjectRelease, type ReleaseInfo } from "@/lib/release";

const latestVersionUrl = `${GITHUB_RAW_URL}/VERSION`;
const latestChangelogUrl = `${GITHUB_RAW_URL}/CHANGELOG.md`;

function readLocalReleases(): ReleaseInfo[] {
  return JSON.parse(process.env.NEXT_PUBLIC_APP_RELEASES || "[]");
}

function toVersionParts(version: string) {
  const match = version.trim().match(/^v?(\d+)\.(\d+)\.(\d+)/);
  return match ? match.slice(1).map(Number) : null;
}

function isNewerVersion(latestVersion: string, currentVersion: string) {
  const latest = toVersionParts(latestVersion);
  const current = toVersionParts(currentVersion);
  if (!latest || !current) return false;
  return latest.some(
    (value, index) =>
      value > current[index] &&
      latest.slice(0, index).every((part, prevIndex) => part === current[prevIndex]),
  );
}

export function useVersionCheck() {
  const currentVersion = webConfig.appVersion;
  const localReleases = useMemo(readLocalReleases, []);
  const [latestVersion, setLatestVersion] = useState(currentVersion);
  const [releases, setReleases] = useState<ReleaseInfo[]>(localReleases);
  const [checking, setChecking] = useState(false);
  const [open, setOpen] = useState(false);
  const hasNewVersion = isNewerVersion(latestVersion, currentVersion);

  const checkLatestRelease = useCallback(
    async (showMessage = false) => {
      setChecking(true);
      try {
        const [versionResponse, changelogResponse] = await Promise.all([
          fetch(latestVersionUrl),
          fetch(latestChangelogUrl),
        ]);
        if (!versionResponse.ok || !changelogResponse.ok) throw new Error();
        const [version, changelog] = await Promise.all([
          versionResponse.text(),
          changelogResponse.text(),
        ]);
        const release = parseProjectRelease(version, changelog);
        setLatestVersion(release.version);
        setReleases(release.releases);
        if (showMessage) toast.success("已获取最新版本信息");
      } catch (error) {
        setLatestVersion(currentVersion);
        setReleases(localReleases);
        if (showMessage) toast.error(error instanceof Error && error.message.startsWith("仓库")
          ? error.message
          : "获取最新版本信息失败");
      } finally {
        setChecking(false);
      }
    },
    [currentVersion, localReleases],
  );

  const openReleaseModal = () => {
    setOpen(true);
    void checkLatestRelease();
  };

  return {
    open,
    setOpen,
    openReleaseModal,
    latestVersion,
    releases,
    checking,
    hasNewVersion,
    checkLatestRelease,
  };
}
