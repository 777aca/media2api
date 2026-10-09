# 账号权重与优先级

在「号池管理」列表查看账号的优先级和权重，点击该行「编辑账户」修改并保存。保存后对后续账号选取生效，已开始的请求继续使用原账号。

| 字段 | 默认值 | 取值 | 含义 |
| --- | --- | --- | --- |
| `priority`（优先级） | 0 | 0–1000 的整数 | 数值越大越先使用 |
| `weight`（权重） | 1 | 1–1000 的整数 | 同优先级账号之间的相对请求份额 |

例如，两个账号优先级都为 10，权重分别为 3 和 1，在候选账号持续可用时，长期选取比例为 3:1。优先级为 0 的账号在更高优先级账号没有满足本次请求条件的可用候选时才会被选取；高优先级账号持续可用时，低优先级账号不会获得请求。需要停用账号时修改账号状态为「禁用」。

## 调度规则

1. 保留既有可用性过滤：图片请求检查状态、图片额度、套餐、来源、并发槽位和本次重试排除名单；文本请求检查既有状态条件、模型与通道权限和重试排除名单。图片限流状态继续沿用现有文本调度规则，不额外禁止文本调用。
2. 从筛选后的候选中取最高优先级。同级使用平滑加权轮询，默认权重均为 1 时等量轮询。
3. 图片并发槽位已满的高优先级账号可回退到仍有空闲槽位的低优先级账号。远程校验失败后仍走现有重试与槽位释放流程。
4. 图片、网页文本、Codex 文本分别维护轮询计分，计分仅存于当前服务进程。重启后计分重置，账号设置保留。资格、并发或重试导致候选集合变化时，实际比例可能变化。
5. 账号设置随现有账号 JSON 数据持久化，兼容 JSON、Git 和数据库存储，不增加数据库列。凭据刷新、更换 token、未提供这些字段的重复导入及其他账号信息更新均保留设置。旧账号缺失字段时补默认值；旧存储或导入文件中无效字段使用默认值。

## API

管理员通过 `POST /api/accounts/update` 更新已有账号：

```json
{
  "access_token": "example-account-token",
  "priority": 10,
  "weight": 3
}
```

两个字段均可独立省略；省略或传 `null` 保留原值。更新接口拒绝越界、布尔值、小数及字符串数字，返回 HTTP 422，原设置不变。`GET /api/accounts` 和更新响应的账号对象包含数值类型的 `priority`、`weight`。

## 验证

```powershell
.venv\Scripts\python.exe scripts\run_model_catalog_tests.py test.test_account_scheduling test.test_account_image_capabilities test.test_text_model_routing test.test_account_identity test.test_account_text_import_payload
```

该入口在临时副本使用示例配置、隔离存储并封锁出站网络。前端在 `web/` 运行 `bun test src/lib/account-scheduling.test.ts`、`bunx tsc --noEmit` 和 `bun run build`。
