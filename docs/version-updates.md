# Docker 版本更新

管理员点击导航栏版本号，可检查正式版本、一键更新并回滚到上一次成功运行的版本。检查结果缓存 5 分钟，“检查更新”立即重新查询。更新任务独立执行，关闭页面不影响任务；服务切换期间页面自动重新连接。

## 首次启用

需要 Linux Docker Engine（或 Docker Desktop 的 Linux 容器模式）和 Docker Compose。当前源码开发服务只提供版本检查；不会自动执行 git pull 或重启本地开发进程。

在已配置 `config.json` 和 `data/` 挂载的项目目录执行：

```bash
docker compose -f docker-compose.yml -f docker-compose.update.yml up -d --build
```

正式镜像发布后，也可直接使用镜像：

```bash
docker compose -f docker-compose.yml -f docker-compose.update.yml pull app updater
docker compose -f docker-compose.yml -f docker-compose.update.yml up -d --no-build
```

WARP 部署在 `.env` 增加 `MEDIA2API_UPDATE_CONTAINER=media2api-warp`，并将以上主 Compose 文件改为 `docker-compose.warp.yml`。自定义容器名时也要同步设置此变量。容器内服务端口默认为 80，自定义端口使用 `MEDIA2API_UPDATE_HEALTH_PORT`；这不是宿主机映射端口。

更新服务单独挂载 Docker socket，有控制宿主机 Docker 的权限；业务容器不挂载 socket。更新服务不开放网络端口，只通过独立命名卷收取固定格式任务。只允许管理员调用更新接口，目标容器必须带管理标签，更新镜像固定为本仓库发布的 GHCR 摘要。不要将状态卷共享给其他应用，其中包含恢复原容器所需的环境配置。

## 更新与回滚

1. 等待正在生成的图片任务结束，再进入版本弹窗检查更新。
2. 点击“一键更新”，确认目标版本。镜像下载期间旧服务继续运行。
3. 下载和镜像来源校验完成后，替换业务容器，保留端口、环境变量、网络别名、重启策略及数据卷。
4. 在新容器内检查 `/version`，要求 120 秒内返回目标版本。失败自动恢复原版本；成功后可刷新页面加载新前端。
5. “回滚至 v…”使用上一次成功运行的容器配置和本地镜像；回滚同样检查服务启动情况。

状态卷记录任务及恢复信息。更新服务异常退出后重新启动，会优先恢复中断前的容器。每次更新只保留一个回滚点。回滚恢复应用程序和容器配置，不回退图片、账号或数据库数据；未来含不兼容数据迁移的版本须先单独备份。不要删除状态卷、执行 `down -v` 或清理需要回滚的旧镜像。

网页更新直接切换运行中的容器，不修改宿主机 Compose 文件。之后手工运行 `docker compose up` 可能按 Compose 中的旧标签重新创建应用；手工维护前请把 `app.image` 对齐目标版本/摘要，或显式拉取希望部署的版本。更新服务自身需要通过 Compose 更新，业务容器的一键更新不会更换 updater。

当前不支持 `network_mode: container:...`、自动删除容器、多副本应用或多个更新服务管理同一业务容器。所有更新写操作串行执行；旧版无更新接口的镜像需先通过 Compose 升级一次。

## 发布版本

更新目录读取 `777aca/media2api` 的 GitHub 正式 Release。不会将 main 分支的未发布提交当作新版本，也不读取其他项目的版本号。

发布前同步 `VERSION`、`pyproject.toml`、`uv.lock`、`web/package.json` 以及 `CHANGELOG.md`。`Dockerfile` 的 `APP_VERSION` 默认值也应同步；CI 始终显式传入 VERSION。运行：

```bash
python scripts/prepare_release.py --tag v0.1.0
```

维护者按正常发布流程推送与 VERSION 匹配的 `vX.Y.Z` 标签。GitHub Actions 校验版本、发布 amd64/arm64 业务镜像、updater 镜像与可选超分 Worker 镜像，然后创建正式 Release，附带 `media2api-release.json`，其字段为 `schema`、`version`、`image`（业务镜像的不可变 SHA-256 摘要）。超分 Worker 从 v0.1.3 开始提供，使用独立的 `super-resolution-X.Y.Z` 标签，不包含模型文件；页面一键更新仅管理业务容器，Worker 由 Compose 单独升级。首次发布后需将 GHCR 软件包设为公开；当前更新服务只支持公开镜像，不读取宿主机 Docker 登录凭据。仅推送 main 或手动构建镜像不会创建正式 Release。此功能的代码修改本身不会发布版本。

## 排障与恢复

- “暂未发布正式版本”：还没有 GitHub Release，先完成首个版本发布。
- GitHub 访问限制/网络错误：保留上次成功信息并显示原因，稍后重新检查；失败时禁止发起新的更新，已有回滚仍可用。
- 更新服务未就绪：检查 `docker compose -f docker-compose.yml -f docker-compose.update.yml logs updater` 和 Docker socket/状态卷挂载。
- 自动恢复失败：先检查磁盘空间、Docker 引擎和日志，再重启 updater 让其重试恢复。恢复日志不输出账号密钥或容器环境值。
- 需要手工恢复：先停止 updater，使用已知旧版镜像标签/摘要修改主 Compose 的 `app.image`，然后仅重新创建 app；保留原 data、config.json 和更新状态卷。确认服务恢复后再检查状态卷中的 `journal.json`，避免遗留任务在重启 updater 时再次恢复旧容器。

管理员接口：`GET /api/system/update`、`POST /api/system/update/check`、`GET /api/system/update/status`，以及 `POST /api/system/update/apply` / `rollback`（请求体 `{"version":"X.Y.Z"}`）。非管理员返回 403，未登录返回 401；不支持或冲突的写操作返回 409。

交互流程参考 [sub2api 更新服务](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/update_service.go)，本项目使用独立 Docker 执行器替换容器。
