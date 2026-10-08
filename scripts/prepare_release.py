"""校验版本一致性，并生成 Docker 更新清单与发布说明（不发布）。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.docker_update.protocol import IMAGE, PROTOCOL, validate_image, version_parts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="")
    parser.add_argument("--digest", default="")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    version = (ROOT / "VERSION").read_text().strip()
    version_parts(version)
    assert tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"] == version, "Python 版本不一致"
    assert json.loads((ROOT / "web/package.json").read_text(encoding="utf-8"))["version"] == version, "前端版本不一致"
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert next(item["version"] for item in lock["package"] if item["name"] == "media2api") == version, "锁文件版本不一致"
    if args.tag:
        assert args.tag == f"v{version}", "Git 标签与 VERSION 不一致"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = next((part for part in changelog.split("\n## ") if part.startswith(f"{version} - ")), None)
    assert changelog.startswith("# media2api Changelog") and section, "缺少本版本更新说明"
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"version={version}\n")
    if args.digest:
        assert args.output is not None, "请指定输出目录"
        image = validate_image(f"{IMAGE}@{args.digest}")
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "media2api-release.json").write_text(json.dumps({"schema": PROTOCOL, "version": version, "image": image}, indent=2), encoding="utf-8")
        (args.output / "release-notes.md").write_text(section.partition("\n")[2].strip(), encoding="utf-8")
    print(f"版本校验通过：{version}")


if __name__ == "__main__":
    main()
