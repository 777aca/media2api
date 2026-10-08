<h1 align="center">media2api</h1>

<p align="center">media2api 主要是对 ChatGPT 官网相关能力进行逆向整理与封装，提供面向 ChatGPT 图片生成、图片编辑、多图组图编辑场景的 OpenAI 兼容图片 API / 代理，并集成在线画图、号池管理、多种账号导入方式与 Docker 自托管部署能力。</p>

当前版本：[0.1.0](./VERSION) · [GitHub 仓库](https://github.com/777aca/media2api) · [更新日志](./CHANGELOG.md)

> [!WARNING]
> 免责声明：
>
> 本项目涉及对 ChatGPT 官网文本生成、图片生成与图片编辑等相关接口的逆向研究，仅供个人学习、技术研究与非商业性技术交流使用。
>
> - 严禁将本项目用于任何商业用途、盈利性使用、批量操作、自动化滥用或规模化调用。
> - 严禁将本项目用于破坏市场秩序、恶意竞争、套利倒卖、二次售卖相关服务，以及任何违反 OpenAI 服务条款或当地法律法规的行为。
> - 严禁将本项目用于生成、传播或协助生成违法、暴力、色情、未成年人相关内容，或用于诈骗、欺诈、骚扰等非法或不当用途。
> - 使用者应自行承担全部风险，包括但不限于账号被限制、临时封禁或永久封禁以及因违规使用等所导致的法律责任。
> - 使用本项目即视为你已充分理解并同意本免责声明全部内容；如因滥用、违规或违法使用造成任何后果，均由使用者自行承担。
> - 本项目基于对 ChatGPT 官网相关能力的逆向研究实现，存在账号受限、临时封禁或永久封禁的风险。请勿使用你自己的重要账号、常用账号或高价值账号进行测试。

## 快速开始

### Docker 运行

```bash
git clone https://github.com/777aca/media2api.git
cd media2api
cp config.example.json config.json
```

`config.example.json` 不含实际账号或存储凭据，默认管理员密钥为 `media2api`。实际部署时请在 `config.json` 中自定义 `auth-key`，也可以在 `docker-compose.yml` 中通过 `MEDIA2API_AUTH_KEY` 覆盖。运行配置 `config.json` 被 Git 忽略；后续更新时保留已有配置，不要再次复制覆盖。

从当前源码构建并运行：

```bash
docker compose up -d --build
```

此方式不依赖 `ghcr.io/777aca/media2api:latest` 已发布，但构建仍需下载基础镜像和依赖。确认目标镜像已发布且可访问后，也可以使用镜像运行：

```bash
docker compose pull app
docker compose up -d --no-build --pull never
```

Windows PowerShell 下可用 `Copy-Item config.example.json config.json` 复制配置。

- Web 面板：`http://localhost:3000`
- API 地址：`http://localhost:3000/v1`
- 数据目录：`./data`

### WARP / FlareSolverr 稳定代理部署

如果图片链路经常遇到 Cloudflare 拦截，可以启用附带的 WARP + Privoxy + FlareSolverr 方案：

```bash
cp config.example.json config.json
cp .env.example .env
docker compose -f docker-compose.warp.yml up -d --build
```

首次部署请在 `.env` 中设置 `MEDIA2API_AUTH_KEY`；已有配置和 `.env` 应继续复用。

该 compose 会启动：

- `warp-proxy`：提供 WARP SOCKS5 出口。
- `privoxy`：把 WARP SOCKS5 转成 HTTP 代理。
- `flaresolverr`：刷新 Cloudflare clearance。
- `init-config`：幂等写入 `proxy_runtime` 默认配置。
- `app`：启动 media2api 主服务。

默认只让上游 OpenAI / ChatGPT 请求走稳定代理，账号邮箱、CPA 等辅助链路不会被强制接管。账号自身配置的代理优先级最高，其次是稳定代理运行时，再其次是显式代理和旧版全局代理。

可在 `.env` 中调整端口和代理运行时参数，也可在后台设置页的「稳定代理运行时」面板手动保存、测试代理和测试 clearance。

### 本地开发

启动后端：

```bash
git clone https://github.com/777aca/media2api.git
cd media2api
cp config.example.json config.json
```

默认管理员密钥为 `media2api`，可在 `config.json` 中自定义 `auth-key`。启动后端：

```bash
uv sync
uv run main.py
```

另开终端，在项目根目录启动前端：

```bash
cd web
bun install
bun run dev
```

前端开发服务默认使用 `7000` 端口，访问 `http://localhost:7000`。

如果默认后端端口 `8000` 被占用，可用 `uv run uvicorn main:app --host 127.0.0.1 --port 8001` 启动后端，并在 `web/.env.local` 中设置 `NEXT_PUBLIC_API_URL=http://127.0.0.1:8001`，前端开发服务会读取该地址。

从源码构建的部署，后续更新：

```bash
git pull
docker compose up -d --build
```

已发布镜像部署，确认新镜像可用后更新：

```bash
docker compose pull app
docker compose up -d --no-build --pull never
```

### 存储后端配置

支持通过环境变量 `STORAGE_BACKEND` 切换存储方式：

- `json` - 本地 JSON 文件（默认）
- `sqlite` - 本地 SQLite 数据库
- `postgres` - 外部 PostgreSQL（需配置 `DATABASE_URL`）
- `git` - Git 私有仓库（需配置 `GIT_REPO_URL` 和 `GIT_TOKEN`）

示例：使用 PostgreSQL

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

## 功能

### API 兼容能力

- 兼容 `POST /v1/images/generations` 图片生成接口
- 兼容 `POST /v1/images/edits` 图片编辑接口
- 兼容面向图片场景的 `POST /v1/chat/completions`
- 兼容面向图片场景的 `POST /v1/responses`
- `GET /v1/models` 仅返回已接入且当前账号池符合接入条件的图片型号，根据本地账号状态即时更新，不拉取上游文本模型目录。
- 账号页与设置页共享图片模型列表，支持刷新目录、更新时间与失败提示；详情见[图片模型目录与账号权限](./docs/model-catalog.md)。
- 支持通过 `n` 返回多张生成结果
- 支持生成可编辑 PPT 文件
- 支持生成可编辑 PSD 文件
- 账号池存在 Codex 来源的 `Plus` / `Team` / `Pro` 账号时，提供 `codex-gpt-image-2` 及对应套餐入口；实际调用权限和额度由上游校验。

### 在线画图功能

- 内置在线画图工作台，支持生成、图片编辑与多图组图编辑
- 支持 `gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`，以及符合账号条件时的 `codex-gpt-image-2` 与套餐兼容入口
- 编辑模式支持参考图上传
- 前端支持多图生成交互
- 本地保存图片会话历史，支持回看、删除和清空
- 支持服务端缓存图片URL
- 图片生成进度追踪，超时后可继续等待
- 图片懒加载与滚动位置记忆，优化大量图片场景性能

### 号池管理功能

- 自动刷新账号邮箱、类型、额度和恢复时间（异步进度追踪）
- 轮询可用账号执行图片生成与图片编辑
- 遇到 Token 失效类错误时自动剔除无效 Token
- 定时检查限流账号并自动刷新
- 支持密码重新登录恢复异常账号，刷新后可自动重登
- 支持网页端配置全局 HTTP / HTTPS / SOCKS5 / SOCKS5H 代理
- 支持 WARP / FlareSolverr 稳定代理运行时
- 支持搜索、筛选、批量刷新、导出、手动编辑和清理账号
- 支持四种导入方式：本地 CPA JSON 文件导入、远程 CPA 服务器导入、`sub2api` 服务器导入、`access_token` 导入
- TXT 上传和粘贴兼容 `邮箱----密码----二步验证密钥----Access Token`，也兼容「卡密 N:」前缀和逐行 Token，详见[账号文本导入](./docs/account-import.md)。
- 支持在设置页配置 `sub2api` 服务器，筛选并批量导入其中的 OpenAI OAuth 账号

### 图片管理

- 支持图片预览、日期与标签筛选、批量下载和删除。
- 支持统一设置本地图片保留时长，最低 1 小时，到期自动清理，保留生成记录和调用日志。
- 图片有效期适用于已有和新增本地图片，WebDAV 远程副本不受影响。

## 项目文档

- [部署与升级](./docs/deployment.md)
- [账号文本导入](./docs/account-import.md)
- [图片 2.5 网页生图接入](./docs/image-models-2.5.md)
- [图片有效期](./docs/image-retention.md)

## API

所有 AI 接口都需要请求头：

```http
Authorization: Bearer <auth-key>
```

<details>
<summary><code>GET /v1/models</code></summary>
<br>

返回当前暴露的图片模型列表。

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer <auth-key>"
```

<details>
<summary>说明</summary>
<br>

| 字段     | 说明                                                                                                               |
| :------- | :----------------------------------------------------------------------------------------------------------------- |
| 返回模型 | `gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`，以及满足账号条件时的 Codex 图片入口；以实际响应为准 |
| 接入场景 | 可接入 Cherry Studio、New API 等上游或客户端                                                                       |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/generations</code></summary>
<br>

OpenAI 兼容图片生成接口，用于文生图。

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "一只漂浮在太空里的猫",
    "n": 1,
    "response_format": "b64_json"
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段              | 说明                                                                     |
| :---------------- | :----------------------------------------------------------------------- |
| `model`           | 图片模型，当前可用值以 `/v1/models` 返回结果为准，推荐使用 `gpt-image-2` |
| `prompt`          | 图片生成提示词                                                           |
| `n`               | 生成数量，当前后端限制为 `1-4`                                           |
| `response_format` | 当前请求模型中包含该字段，默认值为 `b64_json`                            |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/edits</code></summary>
<br>

OpenAI 兼容图片编辑接口，可上传图片文件，也可按官方 JSON 格式传入图片链接并生成编辑结果。

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -F "model=gpt-image-2" \
  -F "prompt=把这张图改成赛博朋克夜景风格" \
  -F "n=1" \
  -F "image=@./input.png"
```

也可以直接传图片 URL：

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "把这张图改成赛博朋克夜景风格",
    "images": [
      {"image_url": "https://example.com/input.png"}
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段        | 说明                                                   |
| :---------- | :----------------------------------------------------- |
| `model`     | 图片模型， `gpt-image-2`                               |
| `prompt`    | 图片编辑提示词                                         |
| `n`         | 生成数量，当前后端限制为 `1-4`                         |
| `image`     | 需要编辑的图片文件，使用 multipart/form-data 上传      |
| `images`    | JSON 图片引用数组，支持 `{"image_url": "https://..."}` |
| `image_url` | 表单模式下也可直接传图片链接，支持重复字段传多张图     |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/chat/completions</code></summary>
<br>

面向文本、网页搜索与图片场景的 Chat Completions 兼容接口，不是完整通用聊天代理。

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "messages": [
      {
        "role": "user",
        "content": "生成一张雨夜东京街头的赛博朋克猫"
      }
    ],
    "n": 1
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                 | 说明                                                                               |
| :------------------- | :--------------------------------------------------------------------------------- |
| `model`              | 文本、搜索或图片模型；搜索模型会触发网页搜索兼容逻辑                               |
| `messages`           | 消息数组，支持文本、搜索和图片请求内容                                             |
| `n`                  | 图片生成数量，按当前实现解析为图片数量                                             |
| `stream`             | 文本、搜索和图片场景均支持，仍在测试                                               |
| `tools`              | 文本场景支持 `web_search` / `web_search_preview` / `web_search_preview_2025_03_11` |
| `web_search_options` | 传入时会触发网页搜索兼容逻辑                                                       |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/responses</code></summary>
<br>

面向文本、网页搜索和图片生成工具调用的 Responses API 兼容接口，不是完整通用 Responses API 代理。

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "input": "生成一张未来感城市天际线图片",
    "tools": [
      {
        "type": "image_generation"
      }
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段     | 说明                                                                                         |
| :------- | :------------------------------------------------------------------------------------------- |
| `model`  | 响应中会回显该模型字段，搜索和图片生成会走对应兼容逻辑                                       |
| `input`  | 输入内容；搜索使用最后一条用户文本，图片生成需能解析出提示词                                 |
| `tools`  | 支持 `image_generation`、`web_search`、`web_search_preview`、`web_search_preview_2025_03_11` |
| `stream` | 已实现，但仍在测试                                                                           |

<br>
</details>
</details>

## 反馈与贡献

使用问题和功能建议请提交到 [本仓库 Issues](https://github.com/777aca/media2api/issues)，代码改进可以提交 Pull Request。
