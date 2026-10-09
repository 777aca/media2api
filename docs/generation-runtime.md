# 生图队列、额度与运行统计

本次改造覆盖网页生图任务、Images generations/edits、Chat Completions 生图和 Responses 生图。纯文本、搜索、可编辑文件生成继续使用原执行流程。升级不修改现有账号优先级（数字越大越优先）和权重规则。

## 配置与部署

`config.example.json` 新增 `image_queue`。旧配置缺少此字段时使用以下默认值，可在后台“设置 → 基础配置 → 生图调度”修改并保存：

```json
{
  "image_queue": {
    "global_concurrency": 8,
    "key_concurrency": 4,
    "max_waiting_images": 200,
    "queue_timeout_seconds": 600,
    "task_retention_days": 30
  }
}
```

- 并发与等待容量按图片子任务计数，多图请求原子入队，容量或额度不足时整次拒绝。
- 使用常驻执行池；全局执行名额直到结果保存、后处理和结算完成才释放。原账号并发限制继续生效。
- 全局并发、调用 Key 并发和上游账号并发在唯一调度器中联合准入。账号繁忙或冷却时，任务仍为 `queued`，不占全局/Key 执行名额、不占工作线程，继续计入等待容量、超时和取消范围。
- `image_account_concurrency` 保留原字段与原值，表示每个上游 ChatGPT/Codex 账号的并发上限；它与 `image_queue.key_concurrency`（调用者并发）在同一“生图调度”区域编辑。
- Key 之间轮转，同一个 Key 内按提交顺序安排。降低并发不会中断已开始的任务，后续任务等待占用下降。
- 账号不足的 Key 不阻塞其他 Key 的可执行任务；同 Key 的队首仍保持顺序。安全换号重试先释放执行和账号名额，再回到同一队列，失败账号 ID 持久化，重启不会重置最多 3 个不同账号的尝试上限。
- `global_concurrency`、`key_concurrency` 支持 1–64，等待容量支持 1–10000。任务输入至少保留 30 天；未结束任务不会清理。
- **只支持单实例、单 API worker。** 数据目录通过操作系统文件锁独占；重复调度器启动会明确报错。不要使用多 worker 或多个容器共享同一数据目录。
- 容器必须持久化整个 `data/` 目录；SQLite 采用 WAL 和 FULL 同步。正常关闭等待在途执行结束；进程被强制停止后由检查点恢复。

`data/generation-runtime.sqlite` 保存请求、图片任务、额度预占、唯一结算流水和统计事件。参考图、合成蒙版、已取得原图与结果检查点放在 `data/generation-tasks/<内部任务 ID>/`；该目录没有公开下载路由。最终图片继续使用已有图片存储配置。

## 错误处理与恢复

错误优先按 HTTP 状态及结构化错误码分类，文本仅作为补充：

| 类别 | 处理 |
| --- | --- |
| `rate_limited` | 优先使用 Retry-After 或结构化恢复时间，限制在 60 秒至 7 天，缺省 180 秒 |
| `transient` | 30 秒开始指数退避，最高 900 秒；成功清除连续故障计数 |
| `credentials_invalid` | 明确失效时最多刷新一次；仍失效标记异常，不新增自动删除 |
| `model_permission` | 暂停该账号对应生图通道和型号，保留其他能力 |
| `access_denied` | 未分类 403 仅冷却对应通道 300 秒 |
| `invalid_request` / `content_rejected` | 立即失败，不冷却账号、不换号 |
| 结果轮询、下载、保存、后处理故障 | 恢复原结果，禁止重新生成；超分失败仍使用已有原图回退 |

只有确定未提交（包括明确的连接建立失败）或明确被上游拒绝的请求才允许换号，每张最多 3 个不同账号。读取超时、提交后 5xx、丢失响应不能证明没有生成，因此不重新提交。账号页显示冷却原因、剩余时间、权限暂停范围，管理员可“解除限制”；到期不会绕过禁用、额度、型号权限或账号并发限制。

检查点覆盖账号选择、提交意图、Web 会话句柄、原图取得和输出保存：

| 重启时状态 | 恢复行为 |
| --- | --- |
| 尚未提交 | 重新排队，检查 Key、预占和排队期限 |
| 已提交，有 Web 会话句柄 | 用原账号读取原会话；不换号生成 |
| 已保存原图 | 继续存储/后处理，不调用生图 |
| 已保存输出 | 幂等结算，不重复扣额 |
| 已提交，无法查询 | `uncertain`（结果待确认），保留预占，禁止自动重发 |

运行中取得句柄或原图后遇到结果故障，会自动尝试一次结果恢复；仍失败则保留待确认状态。网页可以继续读取具备句柄或本地检查点的原任务。Codex 生图当前未提供可用于重启查询的句柄；在输出持久化前中断可能进入待确认。

管理员在“运行统计 → 结果待确认”中结束任务后释放预占，且不再交付该任务后续结果。客户端关闭页面或断开 SSE 不会取消已提交任务。等待任务可主动取消。

## API Key 额度

创建、编辑 `/api/auth/users` 支持：

```json
{
  "name": "设计用户",
  "image_quota_limit": 500,
  "image_concurrency_limit": 4
}
```

`image_quota_limit: null` 表示不限额，`0` 不允许接受新图片；`image_concurrency_limit: null` 使用全局默认单 Key 并发。并发必须为 1–64 的整数。列表及编辑结果增加：

- `image_quota_used`：累计成功保存并可交付的图片张数。
- `image_quota_reserved`：已接受但未结算的预占张数，包括待确认任务。
- `image_quota_remaining`：上限减去已用与预占；不限额时为 `null`。

旧 Key 默认不限额，从启用后开始计量，不追溯历史。管理员主密钥不限图片额度，但遵守全局和默认单 Key 并发。更换密钥字符串保留稳定 Key ID、额度与历史；删除 Key 不删除结算流水。降低上限不得低于已用与预占之和。

接受任务时原子预占请求张数，成功保存后逐张结算，失败或取消释放对应预占。部分成功只扣实际成功部分。上游返回更多图片时，在同一事务中检查额外可用额度，只交付额度允许的图片，不占用其他任务的预占。任务 ID 是唯一结算标识，重复完成与恢复不会重复扣额。超分、下载及重复查询不额外扣额。

## 接口增量

- 四种兼容生图入口支持可选 `Idempotency-Key`（最多 256 字符）。作用域为 Key ID + 入口；同标识、同规范化内容复用任务，内容冲突返回 HTTP 409 `idempotency_conflict`。记录至少保留 30 天。
- 生图响应通过 `X-Image-Task-Id` 返回服务端任务 ID；CORS 已暴露该响应头。同步和流式保留各自协议格式，Responses 流中的失败使用 `response.failed`。
- 网页继续使用 `client_task_id`。可同时传 `client_task_ids` 数组一次接受多张（最多 16）；返回 `{ "items": [...] }`，各子任务可按客户端 ID 查询与取消。编辑表单中该数组使用 JSON 字符串。
- `GET /api/image-tasks?ids=...` 只查询自己的任务；支持服务端任务 ID 和网页客户端 ID。状态增加 `uncertain`、`cancelled`；查询包含 `phase`、`queue_seconds`、`error_category`、`error_code`、`recovery_status`、子任务状态和尝试次数。
- `POST /api/image-tasks/{task_id}/cancel`：取消尚未提交的等待任务，释放预占。
- `POST /api/image-tasks/{task_id}/resume-poll`：读取原任务结果；已有请求体保持兼容，恢复执行使用统一调度和固定结果查询超时。
- `GET /api/image-quota`：只返回当前身份额度。
- 管理员接口：`GET /api/runtime/statistics?days=1&model=&channel=&key_id=`、`GET /api/runtime/tasks`、`POST /api/runtime/tasks/{task_id}/end`、`POST /api/accounts/{account_id}/clear-image-cooldown`。

本地拒绝码区分 `image_quota_exceeded`、`image_queue_full`、`image_queue_timeout`、`image_key_disabled`。HTTP 流已开始后通过对应协议错误事件返回失败。任务取消只影响仍在等待的子任务，不撤销已提交的图片。

## 统计口径

管理员运行统计页支持最近 24 小时、7 天、30 天及型号、通道、Key 筛选。实时区展示执行数、等待数、冷却账号数、待确认数、容量和每个 Key 占用。

统计从启用后任务和结算记录计算，不读取调用日志，删除日志不会改变统计或额度。请求数包括接受的请求及记录的入队前拒绝（参数校验、内容审核、额度/容量限制）；成功图片按实际结算张数；部分成功按请求计数，失败、取消、待确认按子任务计数。

平台成功率 = 成功终态图片任务 ÷（成功终态图片任务 + 平台故障终态图片任务）。用户参数错误、内容拒绝、本地拒绝、取消与待确认不进入分母；没有样本显示“暂无数据”。排队耗时从接受到开始执行，生成耗时包括结果读取、保存和后处理，展示 P50/P95。请求筛选时间使用接受时间。

## 迁移与备份恢复

首次启用只读导入 `data/image_tasks.json`，不会覆盖原文件。旧成功记录不补扣额度、不进入新统计；无法恢复的旧未完成任务保留“服务已重启，未完成的图片任务已中断”说明。导入标识保存在运行数据库，重复启动不会重复迁移。

旧 JSON 任务执行器已从生产代码移除，`services/image_task_service.py` 只保留统一队列入口。原行为保存在测试夹具中用于历史兼容回归。运行数据库自动补充 `attempted_accounts` 字段，保留已有任务和额度流水。

运行数据库存在时，备份总是通过 SQLite Backup API 生成一致性快照，同时包含私有任务目录，与日志/图片备份开关无关。原有账号及 Key 快照继续按配置保存。需要恢复已完成图片时，应开启原有图片备份，或确保远程图片存储仍可读取。

恢复顺序：

1. 停止唯一 API 实例，确认没有其他 worker 使用该数据目录。
2. 在同一备份时间点恢复 `generation-runtime.sqlite`、`generation-tasks/`、Key 快照、账号快照和需要的配置/结果图片。不要将旧实例的 `-wal`、`-shm` 文件混入快照。
3. 使用单 worker 启动，检查运行统计、待确认任务、Key 已用和预占，再恢复对外流量。

不要只恢复 Key JSON 而丢弃运行数据库，否则无法保持额度与幂等历史。升级前先备份现有数据；生产恢复遵循上述停机与单实例启动顺序。

## 本地验证

后端使用隔离目录、示例配置和出站网络拦截：

```powershell
.venv\Scripts\python.exe scripts\run_model_catalog_tests.py test.test_generation_runtime test.test_generation_api test.test_generation_recovery
```

新增用例覆盖 8/4/200 边界、Key 公平性、原子预占、多图部分成功、额外图片、重复结算、Key 轮换和删除、断连、各检查点重启、迁移只读、恢复快照及同步/流式接口。原图回退沿用图片校准测试。前端运行 `bun test src`、独立 `bunx tsc --noEmit`、相关 ESLint 和 `bun run build`。故障注入使用合成数据，不调用真实上游。
