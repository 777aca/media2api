# Grok 原生集成

Grok Web 已作为 Python Provider 合入 media2api。一个 FastAPI 进程、一套 Next.js 页面、一套登录与 API Key 即可提供原有 ChatGPT/Codex 与 Grok 能力，不再需要运行独立 Go 服务。

协议适配参考 [grok2api v3.1.8](https://github.com/chenyme/grok2api/tree/v3.1.8)，固定提交 `e2fd7ab481a30a1965bb61395d437154cf247c92`。移植文件在 `services/providers/`；保留其 [MIT 许可证](licenses/grok2api-MIT.txt)。本次移植范围为 Web SSO 渠道，不包含 Build、Console、语音、Realtime 或图生视频。

## 启动与配置

1. 根目录执行 `uv sync`；已有 `config.json`、环境文件和 `data/` 必须保留。
2. 根目录执行 `uv run main.py`，后端默认 8000。前端在 `web/` 执行 `bun install`、`bun run dev`，访问 `http://127.0.0.1:7000`。Docker 继续使用原有 `docker-compose.yml`。
3. 使用 media2api 管理员登录，在「号池管理 → Grok Web 号池」导入 SSO：每行一个，或粘贴 Web 账号 JSON；点击「同步资料与额度」。可以手动选择 Basic / Super / Heavy 等级，最终权限仍由上游决定。
4. 在「设置 → Grok Web」配置独立代理、签名服务或手动 Statsig 值以及并发限制。默认签名服务为上游项目使用的 `https://grok.wodf.de/sign`；仅发送公开页面签名元数据，不发送账号 Cookie。手动值可能随上游变化失效。
5. 使用「对话」「图片」「视频」页面或下列 API。没有符合账号等级的 Grok 账号时，对应模型不会出现在目录。

账号 JSON 例子（仅示意）：

```json
{"provider":"grok_web","accounts":[{"sso_token":"replace-with-your-sso","tier":"basic","name":"Grok 1","cloudflare_cookies":""}]}
```

SSO、Cloudflare Cookie、代理地址和手动签名值加密保存在 `data/native-providers.sqlite`；加密密钥在 `data/.native-provider-key`。备份与还原时必须同时保留两者。现有备份功能会纳入它们；勾选图片备份也会纳入视频目录。API 不回传原始凭据。聊天历史、Responses 状态与视频任务按 media2api 用户隔离，视频文件位于 `data/videos/`。

## 能力与接口

所有入口使用 `Authorization: Bearer <media2api-api-key>`。

| 能力 | 路由 | 约束 |
| --- | --- | --- |
| 模型目录 | `GET /v1/models` | 原有模型与符合账号条件的 Grok 模型 |
| 对话 | `POST /v1/chat/completions` | 文本、base64 图片、多轮、SSE、函数工具 |
| Responses | `POST /v1/responses` | `previous_response_id` 同用户同模型，`store=false` 不保存 |
| Responses 状态 | `GET/DELETE /v1/responses/{id}` | 本地保存的 Grok Responses |
| Anthropic | `POST /v1/messages` | 文本与工具调用兼容表示 |
| 图片生成 / 编辑 | `POST /v1/images/generations`、`POST /v1/images/edits` | n=1、quality=auto；共用图片队列和额度 |
| 文生视频 | `POST /v1/videos/generations` | 异步任务，可用 `Idempotency-Key` 去重 |
| 视频状态 / 内容 | `GET /v1/videos/{id}`、`GET/HEAD /v1/videos/{id}/content` | 内容支持 Range；需同用户认证或短期播放票据 |

`/grok/v1/...` 是上述 `/v1/...` 的本地兼容别名，使用同一密钥和处理函数，不转发到旧服务。旧客户端需要改用 media2api Key；不承诺完整兼容 grok2api 所有接口或参数。旧代理模块和模拟测试暂留作迁移参考，不在应用中注册。

对话模型包括 `grok-chat-fast`、`grok-chat-auto`、`grok-chat-expert`、`grok-chat-heavy`；图片包括 `grok-imagine-image-lite`、`grok-imagine-image`、`grok-imagine-image-2.0`、`grok-imagine-image-edit`；视频为 `grok-imagine-video`。模型目录会随账号等级过滤。

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer <media2api-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-chat-fast","stream":true,"messages":[{"role":"user","content":"你好"}]}'

curl http://127.0.0.1:8000/v1/videos/generations \
  -H "Authorization: Bearer <media2api-api-key>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: your-unique-request-id" \
  -d '{"model":"grok-imagine-video","prompt":"海面上的日出","duration":6,"aspect_ratio":"16:9","resolution":"720p"}'
```

视频参数：1–15 秒，480p/720p；Basic 仅支持 720p，实际时长限制默认为 6 秒，可由管理员在 1–15 秒内设置。返回 `actual_duration` 表示实际请求时长。Web 不支持图生视频；输入图片时返回 400。

函数工具通过提示词协议适配，不执行用户工具；返回参数会校验为 JSON 对象。Token 用量为本地估算，`usage.estimated=true`，记录到调用日志。聊天与视频仅实施并发控制和调用记录；图片成功后沿用现有图片额度结算。对话图片仅接受 base64 data URL，最多 8 张；工作台保存文字历史，不保存附件。

## 恢复与保留

视频在重启后继续处理未提交任务和已取得结果地址的下载。若提交结果未知，状态变为 `uncertain`，不会自动重复生成。已有结果下载失败时，可从工作台重试下载，或 `POST /api/video-tasks/{id}/retry-download`；没有结果地址的任务不可重试下载。

`retention_days` 默认为 30；启动时清理超期会话、Responses 和已结束视频任务及媒体，不清理仍排队或生成中的任务。播放链接一小时过期，可在工作台刷新。账号失效需重新导入；429 会让账号短暂冷却。上游协议、签名和风控变化可能需要更新适配。

旧 `MEDIA2API_GROK_GATEWAY_URL` 不再使用，`docker-compose.grok.yml` 仅保留空兼容覆盖。旧容器和卷不会自动删除；确认原生接入正常后可自行停用。不会自动读取旧 Go 服务的数据库或加密凭据，使用 Web 账号导出后导入。

## 验证

隔离单元测试（示例配置、合成凭据、禁止出站网络）：

```powershell
.venv\Scripts\python.exe scripts\run_model_catalog_tests.py test.test_grok_native
```

本地真实 HTTP/WebSocket 传输测试：先运行下面的 Postman simulation，再在另一终端执行 Python 脚本。HTTP mock 根据固定版本协议构建；WebSocket stand-in 由脚本创建。只验证本地协议与生命周期，不代表真实 Grok 账号通过风控或生成成功。

```powershell
postman simulation run postman/simulations/grok-web-native.sim.yaml --port 4564 --no-history
.venv\Scripts\python.exe scripts\run_grok_native_simulation.py http://127.0.0.1:4564/web
```
