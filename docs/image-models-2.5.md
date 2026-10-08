# 图片 2.5 网页生图接入

项目使用现有 ChatGPT 账号，将 `gpt-image-2.5-flare` 和 `gpt-image-2.5-sunburst` 接入网页会话生图。只参考 [jiawen-afk/chatgpt2api 的型号映射与网页请求](https://github.com/jiawen-afk/chatgpt2api/blob/6cb88b1391ce0cf9d4835ef788fc13dbc213f179/services/openai_backend_api.py#L684-L693)，不引入该分支的目录同步、默认模型、重试策略或其他功能。

## 型号和调用通道

两个完整型号分别原样放入 `model` 字段，不替换为 `auto`、`gpt-image-2` 或配置中的默认会话模型，也不附加默认思考参数。请求先调用 `/backend-api/f/conversation/prepare`，再调用 `/backend-api/f/conversation`，均携带 `system_hints: ["picture_v2"]`。

`POST /v1/images/generations`、`POST /v1/images/edits`，以及 Chat Completions / Responses 中显式指定这两个型号时，均使用相同的网页生图通道。它们不再调用 Codex 独立 Images 接口。保留的独立接口适配器及其离线测试不作为这两个公开入口的路由。

`gpt-image-2`、`codex-gpt-image-2` 及其已有套餐前缀入口的行为保持原样。裸 `gpt-image-2.5`、未知后缀和未接入快照仍明确拒绝。

## 页面和权限

两个型号在账号页与设置页标记为“网页生图”，管理员目录使用 `source: compatibility`、`entry_kind: web_image`。`GET /v1/models` 保持 OpenAI 格式，不返回内部通道标记。原“项目入口”的含义与表现保持原样。

目录沿用本地活跃账号刷新逻辑，禁用、异常、已删除或空 Token 账号不贡献入口，不额外拉取文本目录。展示入口不代表已确认上游接受该账号的指定型号。Free 账号可以参与网页通道调度，实际型号权限及限额由上游校验；Codex 独立接口的历史 403 不代表网页通道的验证结果。

## 参数和编辑

继续接受项目已有的 `prompt`、`model`、`n`、`size`、`quality`、`response_format` 和参考图。遵循已有串行或并行生成设置，输出复用网页通道的图片解析与存储流程。`size`、`quality` 沿用网页生图的提示词描述，不是独立 Images API 的硬性参数。

网页通道的 `gpt-image-2` 和上述两个 2.5 型号会将有效的像素尺寸展开为宽、高及约分后的宽高比。例如 `1024x2048` 会追加“输出图片目标尺寸：宽 1024 像素、高 2048 像素，宽高比 1:2。请按此宽高比构图。”，用于提高尺寸与构图比例的遵循率，不保证精确像素，也不强制缩放、裁剪返回图片。`auto` 等其他尺寸描述沿用原有提示方式；Codex 通道仍原样传递尺寸参数。

参考图通过网页上传流程传递。单个 mask 要求 PNG、含透明通道且与第一张参考图尺寸一致；其透明度合成到第一张参考图，后续参考图不受影响。多个 mask 仍明确拒绝，其他型号的 mask 行为保持原样。

## 失败与配额

取得图片结果后才扣减本地账号配额，每次调度只结算、释放一个在途槽位。权限、限流和明确型号拒绝保留安全错误代码及上游 HTTP 状态，不将原始响应正文透传给调用方。403 提示网页生图通道拒绝访问；上游明确返回 `model_not_found` / `model_not_available` 时才标记型号权限错误。

只在确认尚未进入结果阶段的连接建立失败时进行有限重试。读取、轮询或结果下载失败不会自动重新提交生成任务，也不自动切换模型或退回 Codex 图片接口。

## 验证范围

离线测试验证两次网页请求的精确型号、思考参数隔离、四类公开入口、mask 合成、错误脱敏、配额与槽位结算、并发部分失败以及结果阶段不重复生成。统一入口为 `scripts/run_model_catalog_tests.py`，使用合成图片与临时配置并封锁出站网络。

本地重启和目录检查不消耗图片额度。尚未发起真实生图，Free 账号能否被上游接受，以及返回图片的实际底层型号，仍需真实调用证据。
