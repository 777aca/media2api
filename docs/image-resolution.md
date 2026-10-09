# 图片尺寸与分辨率校准

网页生图、编辑、Images API、图片流式最终结果和任务续轮询共用结果校准。默认关闭，管理员在「设置 → 基础配置 → 图片分辨率校准」启用并保存后生效。仅处理新生成的结果，已有图片不变。

## 尺寸规则

按照 [OpenAI 图片指南](https://developers.openai.com/api/docs/guides/image-generation#size-and-quality-options)：宽高为 16 的正整数倍，单边不超过 3840，长短边比不超过 3，总像素在 655,360～8,294,400。超过 3,686,400 总像素属于实验性尺寸。上游网页会话仍可能不遵循请求的像素或比例。

常用预设：1024×1024、1536×1024、1024×1536、2048×2048、2048×1152、1152×2048、2560×1440（QHD）、1440×2560（QHD）、3840×2160、2160×3840。官方指南中的 2K 横图为 2048×1152；旧版 2560×1440 仍可通过 QHD 或自定义输入选择。`auto` 真正交由上游决定，不校准。省略 size 保持原接口行为，也不校准。

不合法的显式尺寸在调用生图上游前拒绝。已有历史记录保持不变；复用旧的不合法尺寸时须先调整。页面自定义输入显示校验原因，不自动更改用户输入。

## 校准行为与接口

- 总开关关闭、没有明确目标或上游长边已达到目标时，保持原始字节。
- 上游长边小于目标长边的 2/3 时，通过 Real-ESRGAN 4 倍超分；其他不足目标的情况使用 Pillow Lanczos 缩放。
- 按目标边界等比缩放，不裁剪、不拉伸，不补边。比例不同会使最终宽高小于目标框；4 倍超分后仍偏小时允许继续普通缩放。放大后的文件不是原生高分辨率生成的证明。
- 保留透明通道；处理结果编码为 PNG。模型缺失、连接失败、超时、队列满或输出无效均返回上游原图，不重新发起生成、不重复扣减账号额度。
- 同一次结果中的 URL 与 Base64 使用相同字节；用量估算依据上游图片尺寸，不按后处理增加的像素估算上游消耗。

Images API 的 `data[]` 与任务 `data[]` 增加以下可选字段；Responses 图片输出项携带同名字段：

| 字段 | 含义 |
| --- | --- |
| `requested_size` | 请求尺寸，未指定时为 `auto` |
| `source_size` | 上游图片实际宽高 |
| `actual_size` | 最终文件实际宽高 |
| `processing` | `none`、`resize`、`super_resolution` |
| `processing_status` | `unchanged`、`applied`、`fallback` |
| `processing_error` | 回退时为 `calibration_failed`，不返回内部地址或密钥 |

无法读取尺寸的原始数据不伪造尺寸。历史数据没有处理标记时，页面不推断它是否属于原图。

新失败任务内部保存原账号的稳定标识，续轮询使用该账号当前令牌读取会话，并沿用原请求尺寸。账号标识不加入公开任务响应；旧任务缺少该标识或原账号被删除时明确报错，不猜测账号或自动重新生图。上游会话也必须仍然存在。

管理员配置使用现有 `/api/settings`：

```json
{
  "image_calibration": {
    "enabled": false,
    "worker_url": "http://127.0.0.1:3310",
    "timeout_secs": 300
  }
}
```

`timeout_secs` 为 1～600 的整数。地址不接受 URL 内嵌凭据；鉴权来自环境变量 `SUPER_RESOLUTION_WORKER_SECRET`。`POST /api/image-calibration/test` 接收上述 `image_calibration` 对象、仅检查就绪状态，不保存配置、不执行推理，必须有管理员权限。

## Windows 本地启动

在仓库根目录执行：

```powershell
uv sync --extra super-resolution
.venv\Scripts\python.exe scripts\prepare_super_resolution_model.py
$env:SUPER_RESOLUTION_WORKER_SECRET = "自行设置的共享密钥"
.venv\Scripts\python.exe scripts\run_super_resolution.py
```

Worker 默认监听 `127.0.0.1:3310`，日志为 `logs/super-resolution.log`。主 API 进程也须配置同一个 `SUPER_RESOLUTION_WORKER_SECRET`，设置环境变量后重启对应进程。后台填写本机地址，点击「检查超分连接」，再启用并保存。

本机主 API 与 Worker 共用虚拟环境时，主 API 使用 `uv run --extra super-resolution main.py` 启动，避免普通 `uv run` 自动同步时移除可选推理依赖。Docker 中两个服务使用独立镜像环境。

模型准备脚本是显式网络操作，下载约 4.9 MB，校验 SHA-256 后才保存到 `data/models/realesr-general-x4v3.onnx`。不覆盖不同内容的已有模型。也可自行提供兼容的动态尺寸、RGB float32 NCHW 输入、4 倍 RGB 输出 ONNX 模型，并用 `REALESR_MODEL_PATH` 指定绝对路径。

模型来源为 [GPT2Image-Pro 固定提交中的模型文件](https://github.com/gaoren002/GPT2Image-Pro/blob/22bdcc968ad646f371de632ab6c4a1bdbbd63774/apps/web/models/realesr-general-x4v3.onnx)，SHA-256 为 `027319ffe4f00ec2550957c0957d44969638a03d2ed2f0329af9fd6cd44a457a`。该文件不随本仓库或镜像分发；使用、再分发时保留来源项目及 Real-ESRGAN 的适用许可。模型下载不会在正常应用或 Worker 启动时自动发生。

## Docker

先准备 `data/models` 中的模型，在部署环境设置共享密钥，再执行：

```bash
docker compose -f docker-compose.yml -f docker-compose.super-resolution.yml up -d --build
```

后台服务地址设为 `http://super-resolution:3310`。Worker 仅在 Compose 网络开放，不映射宿主机端口。基础镜像和基础 Compose 不安装或启动推理服务；额外 Compose 启用单独镜像阶段和模型只读挂载。

从 v0.1.3 起，发布流程提供 amd64/arm64 Worker 镜像 `ghcr.io/777aca/media2api:super-resolution`，对应固定版本标签为 `super-resolution-0.1.3`。准备好模型和共享密钥后，也可直接使用已发布镜像：

```bash
docker compose -f docker-compose.yml -f docker-compose.super-resolution.yml pull app super-resolution
docker compose -f docker-compose.yml -f docker-compose.super-resolution.yml up -d --no-build --pull never
```

固定版本部署时，将 app 的镜像标签和 Worker 的版本标签一起更新。管理页面的一键更新仅更新 app 容器；Worker 需通过 Compose 单独升级，模型文件仍由管理员准备。

Worker 使用 ONNX Runtime CPU、默认 2 个计算线程、单并发和最多 8 个等待请求。输入/输出各不超过 64 MiB，输入不超过 8,294,400 像素，分块 256、重叠 16，默认总超时 300 秒。客户端超时可能先于底层当前分块结束，Worker 保持占用推理槽，避免超时请求叠加占用内存。可以用 `SUPER_RESOLUTION_THREADS`（1～32）和 `SUPER_RESOLUTION_TIMEOUT`（1～600）调整运行参数。

排障先检查 `/health/ready`（带 `x-super-resolution-secret` 请求头）与 Worker 日志。应用日志记录校准方式、源/目标尺寸、耗时和失败类别，不记录图片字节或密钥。关闭后台总开关即可立即停止新请求的校准，不影响原有生图通道。

## 验证

普通隔离回归不需要模型，不连接真实服务：

```powershell
.venv\Scripts\python.exe scripts\run_model_catalog_tests.py test.test_image_calibration
```

真实模型验收仅使用合成渐变图，验证 2K/4K、透明度和分块接缝，不调用生图上游：

```powershell
$env:MEDIA2API_TEST_SR_MODEL = (Resolve-Path data/models/realesr-general-x4v3.onnx).Path
$env:MEDIA2API_TEST_SR_OUTPUT = Join-Path (Get-Location).Path "logs/super-resolution-test"
.venv\Scripts\python.exe scripts\run_model_catalog_tests.py test.test_super_resolution_model
```

### 2026-10-09 真实上游验收

经用户授权，通过本地 FastAPI 实际路由调用 `gpt-image-2.5-flare`，仅提交 1 次真实生成请求。路由由 TestClient 驱动，上游请求、Worker HTTP 推理、图片存储及图片读取均使用真实实现；未启动后台定时任务。测试临时启用超分，结束后停止 Worker，`config.json` 内容未改变。

| 项目 | 实测结果 |
| --- | --- |
| 上游原图 | 1672×941 |
| 4K 目标 | 请求 3840×2160，AI 超分后实际 3838×2160 |
| 2K 目标 | 复用同一张上游原图，请求 2048×1152，普通缩放后实际 2047×1152；未再次生成 |
| 耗时 | 真实生成及 4K 返回约 46.9 秒，其中超分约 20.3 秒 |
| 返回一致性 | 生成接口与图片读取均 HTTP 200，URL 读取字节与 Base64 解码字节完全一致 |
| 账号结算 | 本地额度 18→17，成功次数 1→2，在途计数恢复为 0 |
| Usage | 输出 1640 tokens，与上游原图尺寸计算值一致 |
| 故障回退 | 停止 Worker 后用同一原图验证，返回 `fallback`，图片字节与原图完全相同 |

原图宽高比略偏离 16:9，因此按比例完整容纳时宽度少 1～2 像素。这是保持构图、不裁切的结果，返回 `actual_size` 如实表示实际尺寸。4K 结果来自本地 AI 超分，不代表上游原生输出 4K。

脱敏报告及原图、2K/4K 成品保留在本机 `logs/live-calibration-test/`，不纳入 Git。此次验收未覆盖 Docker 部署或浏览器实时生图操作。
