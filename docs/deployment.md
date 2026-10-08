# 部署与升级指南

本文介绍 media2api 的常见部署方式，以及后续升级项目时需要保留的数据和执行步骤。

## 部署前准备

服务器需要安装：

- Docker
- Docker Compose v2
- Git

首次部署前建议确认：

```bash
docker version
docker compose version
git --version
```

项目核心持久化文件：

| 路径 | 作用 |
| --- | --- |
| `config.json` | 主配置、后台密钥、代理、图片、备份等配置 |
| `.env` | Docker compose 环境变量 |
| `data/` | 账号、日志、图片、任务记录等运行数据 |

升级和迁移时重点保留以上内容。

仓库提供不含实际账号或存储凭据的 `config.example.json` 模板，默认管理员密钥为 `media2api`。首次部署先复制为 `config.json`，实际部署时请自定义认证密钥或配置 `MEDIA2API_AUTH_KEY`。运行配置 `config.json` 被 Git 忽略；升级时保留已有配置，不要重复复制覆盖。以下命令使用 Bash 语法，Windows PowerShell 可用 `Copy-Item config.example.json config.json` 复制配置。

## 方式一：普通 Docker 部署

适合不需要 WARP / FlareSolverr 清障的场景。

```bash
git clone git@github.com:777aca/media2api.git
cd media2api
cp config.example.json config.json
```

设置 `config.json` 中的 `auth-key`，或在 `docker-compose.yml` 中配置：

```yaml
environment:
  - MEDIA2API_AUTH_KEY=your_secret_key
```

从当前源码构建并启动：

```bash
docker compose up -d --build
```

默认 Compose 同时保留镜像标识 `ghcr.io/777aca/media2api:latest` 与本地构建配置。`--build` 会构建当前源码，不要求该项目镜像已经发布；构建时仍需下载基础镜像和依赖。

如果已确认目标镜像发布且可访问，可直接拉取并启动：

```bash
docker compose pull app
docker compose up -d --no-build --pull never
```

访问：

```text
http://localhost:3000
```

API 基础地址：

```text
http://localhost:3000/v1
```

查看日志：

```bash
docker logs -f media2api
```

停止：

```bash
docker compose down
```

## 方式二：WARP / FlareSolverr 部署

适合上游请求经常遇到 Cloudflare 拦截的场景。该方式会启动：

- `warp-proxy`
- `privoxy`
- `flaresolverr`
- `init-config`
- `app`

在已克隆的项目目录中，首次部署复制配置和环境变量模板：

```bash
cp config.example.json config.json
cp .env.example .env
```

至少修改 `.env` 中的：

```text
MEDIA2API_AUTH_KEY=your_secret_key_here
```

启动：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

访问：

```text
http://localhost:3000
```

FlareSolverr 相关配置可以在后台设置页的 `FlareSolverr` tab 中查看和测试。

查看容器状态：

```bash
docker compose -f docker-compose.warp.yml ps
```

查看日志：

```bash
docker logs -f media2api-warp
docker logs -f media2api-flaresolverr
```

停止：

```bash
docker compose -f docker-compose.warp.yml down
```

## 方式三：源码运行

适合本地开发或临时调试。

后端：

```bash
git clone git@github.com:777aca/media2api.git
cd media2api
cp config.example.json config.json
```

默认管理员密钥为 `media2api`，可在 `config.json` 中自定义 `auth-key`，或在当前进程环境中设置 `MEDIA2API_AUTH_KEY`。安装依赖并启动：

```bash
uv sync
uv run main.py
```

前端开发服务：

```bash
cd web
bun install
bun run dev
```

前端开发服务默认使用 `7000` 端口，访问 `http://localhost:7000`。

源码方式运行时，后端默认读取项目根目录的 `config.json` 和 `data/`。

后端默认端口为 `8000`。端口被其他项目占用时，可在项目根目录运行 `uv run uvicorn main:app --host 127.0.0.1 --port 8001`，并在 `web/.env.local` 中设置 `NEXT_PUBLIC_API_URL=http://127.0.0.1:8001`，让前端开发服务连接对应后端。此配置只覆盖开发环境 API 地址，生产构建继续使用同源接口。

## 存储后端

默认使用本地 JSON 文件：

```text
STORAGE_BACKEND=json
```

可选值：

| 值 | 说明 |
| --- | --- |
| `json` | 本地 JSON 文件，默认方式 |
| `sqlite` | 本地 SQLite，通常存放在 `data/accounts.db` |
| `postgres` | 外部 PostgreSQL |
| `git` | Git 私有仓库存储账号数据 |

PostgreSQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

SQLite 示例：

```yaml
environment:
  - STORAGE_BACKEND=sqlite
  - DATABASE_URL=sqlite:////app/data/accounts.db
```

## 升级前备份

升级前建议备份：

```bash
mkdir -p backups
tar -czf backups/media2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

如果没有 `.env`，可以去掉：

```bash
tar -czf backups/media2api-$(date +%Y%m%d-%H%M%S).tgz config.json data
```

也可以在后台设置页配置 Cloudflare R2 备份，用于定时备份关键数据。

## 升级：普通 Docker 部署

进入项目目录：

```bash
cd media2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/media2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

从源码构建的部署，拉取最新代码并重建：

```bash
git pull
docker compose up -d --build
```

使用已发布镜像的部署，确认新镜像可用后更新：

```bash
docker compose pull app
docker compose up -d --no-build --pull never
```

查看状态：

```bash
docker compose ps
docker logs -f media2api
```

## 升级：WARP / FlareSolverr 部署

进入项目目录：

```bash
cd media2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/media2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码并重新构建：

```bash
git pull
docker compose -f docker-compose.warp.yml up -d --build
```

查看状态：

```bash
docker compose -f docker-compose.warp.yml ps
docker logs -f media2api-warp
```

## 升级：源码运行

```bash
cd media2api
git pull
uv sync
```

如果需要重新构建前端静态产物：

```bash
cd web
bun install
bun run build
```

然后按你的进程管理方式重启后端服务。

网页检查版本、一键更新与回滚的启用步骤见 [Docker 版本更新](version-updates.md)。

## 回滚

如果升级后需要回滚代码：

```bash
git log --oneline -n 20
git checkout <旧版本commit>
```

普通 Docker 源码构建部署，切换代码后重新构建：

```bash
docker compose up -d --build
```

使用已发布镜像的部署，代码回退不会自动回退镜像。需先将 `docker-compose.yml` 的镜像标签改为已确认可用的旧版本，再拉取并启动：

```bash
docker compose pull app
docker compose up -d --no-build --pull never
```

WARP / FlareSolverr 部署：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

如果需要恢复数据：

```bash
tar -xzf backups/你的备份文件.tgz
```

恢复数据前建议先停止容器，避免运行中写入覆盖：

```bash
docker compose down
```

或：

```bash
docker compose -f docker-compose.warp.yml down
```

## 常用维护命令

查看容器：

```bash
docker compose ps
```

查看主服务日志：

```bash
docker logs -f media2api
```

查看 WARP 部署主服务日志：

```bash
docker logs -f media2api-warp
```

重启普通部署：

```bash
docker compose restart
```

重启 WARP 部署：

```bash
docker compose -f docker-compose.warp.yml restart
```

清理未使用镜像：

```bash
docker image prune
```

不要直接删除 `data/`、`config.json`、`.env`，除非已经确认有可用备份。
