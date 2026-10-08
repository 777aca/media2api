import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { parseProjectRelease } from "./release";

const changelog = "# media2api Changelog\n\n## 0.1.0 - 2026-10-08\n\n+ [新增] 图片有效期设置\n";

describe("media2api 版本信息", () => {
  it("读取本项目版本与更新日志", () => {
    const result = parseProjectRelease("0.1.0\n", changelog);
    assert.equal(result.version, "0.1.0");
    assert.equal(result.releases[0].items[0].content, "图片有效期设置");
  });

  it("拒绝迁移前版本，避免把旧项目 1.8.0 提示为升级", () => {
    assert.throws(() => parseProjectRelease("1.8.0", "# Changelog\n\n## 1.8.0 - 2026-07-28\n+ [新增] 旧项目功能"), /尚未提供 media2api/);
  });

  it("支持 Windows 换行及后续本项目 1.x 版本", () => {
    const result = parseProjectRelease("1.0.0", changelog.replaceAll("0.1.0", "1.0.0").replaceAll("\n", "\r\n"));
    assert.equal(result.releases[0].version, "1.0.0");
  });

  it("拒绝缺失、格式错误或与日志不匹配的版本", () => {
    for (const version of ["", "<html>404</html>", "0.2.0", "0.1.0-invalid"]) {
      assert.throws(() => parseProjectRelease(version, changelog));
    }
    assert.throws(() => parseProjectRelease("0.1.0", "# media2api Changelog\n"), /不一致/);
  });
});
