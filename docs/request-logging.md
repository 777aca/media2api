# 调用日志请求参数

管理员在「日志管理 → 查看详情 → 请求参数」查看和复制格式化 JSON。新日志的 `detail.request_params` 保存客户端传入参数的独立快照；不补入未传入的默认值，不包含服务内部的回调和图片访问基址。日志记录不改变接口支持的参数，未被接口采用的额外字段也仅用于排查。

记录范围包含图片生成、JSON/表单图片编辑、聊天、Responses、Messages、搜索，以及网页异步生图和 PPT/PSD 任务。通过身份验证和参数解析后，成功、上游失败、内容过滤拒绝及流式结束均保留参数。异步任务启动前建立快照，续轮询日志包含任务 ID、额外等待时长和可用的原始请求。身份验证或请求解析阶段被拒绝的请求不新增调用日志。

日志管理页面及 `GET /api/logs` 的 `time`、调用详情中的 `started_at` 和 `ended_at` 均以 UTC+8 显示，日期筛选也按 UTC+8 的日期执行。新记录在日志文件中保存带 `+08:00` 偏移的时间；接口返回仍使用 `YYYY-MM-DD HH:mm:ss` 格式。旧记录未保存时区，读取时按当前服务器本地时区解释并转换，不会改写原日志文件。如果服务器时区在旧记录写入后发生变化，或日志来自其他时区的服务器，旧记录的时间可能无法准确还原。

## 凭据和图片处理

- 不记录 HTTP 请求头。请求参数内已知的令牌、密码、API Key、Cookie 等字段递归替换为 `[REDACTED]`；URL 中的用户信息、敏感查询参数及片段脱敏。
- 上传文件只记录文件名、媒体类型和字节数。Base64 和 data URI 只保留摘要，不写入图片原文；普通远程图片 URL 保留路径和非敏感查询参数。
- 单个文本最多保留 16,000 字符，单次快照总文本最多 64,000 字符；每个集合最多 100 项，最多 1,000 个节点、10 层嵌套。超出部分使用 `[TRUNCATED]` 标记。
- 参数查看沿用现有管理员权限，任务查询接口不额外暴露参数快照。

旧日志没有 `request_params` 时，页面提示「此日志未记录请求参数」，无法追溯补全。空对象 `{}` 表示已记录的空请求。日志详情中的「完整日志 JSON」可展开查看其他诊断字段。

## 带参考图的 generations 兼容请求

`POST /v1/images/generations` 支持在 JSON 中传入 `images`、`image` 或 `image_url`，
解析规则复用 `/v1/images/edits`：可传 base64 data URL、裸 base64、图片引用对象或数组。
带参考图时走图生图处理，素材按数组顺序进入持久化任务；不传素材时仍为文生图。
空数组、空字符串和无效引用返回 400，不会静默退化为文生图。需要 mask 或 multipart 上传时使用 `/v1/images/edits`。

```json
{
  "model": "gpt-image-2",
  "prompt": "保留参考图中的商品，替换背景",
  "images": ["data:image/png;base64,<图片编码>"],
  "n": 1
}
```

旧版本的 generations 接口会忽略图片字段；日志中出现 `request_params.images` 只代表客户端传入了素材，
不能证明素材进入上游。排查旧任务时需同时检查持久化任务的输入标记和素材文件。
修复不会自动重新执行旧任务；旧日志已省略图片原文，重试需要调用方重新提供素材。

## 离线验证

后端通过项目隔离测试入口运行，使用模拟上游与临时存储，不消耗图片生成额度：

```powershell
.venv/Scripts/python.exe scripts/run_model_catalog_tests.py test.test_request_logging test.test_image_task_service test.test_codex_protocol_errors
```

前端在 `web/` 运行 `bun test src`、`bunx tsc --noEmit` 和 `bun run build`。浏览器使用模拟日志验证新记录、历史记录、复制与详情展开。
