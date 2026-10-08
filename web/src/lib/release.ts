export type ReleaseInfo = {
  version: string;
  date: string;
  items: { type: string; content: string }[];
};

export function parseChangelog(content: string): ReleaseInfo[] {
  return content
    .split(/^## /m)
    .slice(1)
    .map((block) => {
      const [title = "", ...lines] = block.trim().split(/\r?\n/);
      const [, version = title.trim(), date = ""] =
        title.match(/^(.+?)(?:\s+-\s+(.+))?$/) || [];
      return {
        version: version.trim(),
        date: date.trim(),
        items: lines
          .map((line) => line.trim().match(/^\+\s+\[(.+?)\]\s+(.+)$/))
          .filter((match): match is RegExpMatchArray => Boolean(match))
          .map((match) => ({ type: match[1], content: match[2] })),
      };
    })
    .filter((release) => release.items.length);
}

export function parseProjectRelease(versionText: string, changelog: string) {
  const version = versionText.trim();
  // 本项目从 0.1.0 重新编号，远程仍是迁移前数据时不能把 1.x 当成新版本。
  if (!/^\d+\.\d+\.\d+$/.test(version) || !/^# media2api Changelog\r?$/m.test(changelog)) {
    throw new Error("仓库尚未提供 media2api 版本信息");
  }
  const releases = parseChangelog(changelog);
  if (!releases.some((release) => release.version === version)) {
    throw new Error("仓库版本号与更新日志不一致");
  }
  return { version, releases };
}
