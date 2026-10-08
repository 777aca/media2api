# Repository Guidelines

## 项目结构与模块职责

- 后端使用 Python 3.13+ 和 FastAPI：`main.py` 是入口，`api/` 定义路由，`services/` 处理账号、生成任务、上游协议及存储，`utils/` 放公共工具。
- 前端使用 Next.js 16、React 19 和 TypeScript：页面位于 `web/src/app/`，共享组件、Hooks、请求与状态分别位于 `components/`、`hooks/`、`lib/`、`store/`。
- 后端测试位于 `test/`；前端测试与源码相邻。`assets/` 存放静态素材，`scripts/` 存放维护工具，`docs/` 存放正式文档，评审方案放 `discuss/`；`data/` 和 `logs/` 是运行目录。

## 构建与本地开发

以下后端命令在仓库根目录运行，前端命令在 `web/` 运行：

| 命令 | 用途 |
| --- | --- |
| `uv sync` | 安装锁定的 Python 依赖及开发依赖 |
| `uv run main.py` | 启动后端，默认端口 8000 |
| `bun install` / `bun run dev` | 安装前端依赖，启动开发服务 |
| `bun run build` | 静态导出前端到 `web/out/` |
| `bunx eslint src` | 检查前端代码规范 |
| `bunx tsc --noEmit` | 独立检查 TypeScript 类型 |

构建配置跳过类型错误，因此前端改动需要独立类型检查。后端换端口时，在 `web/.env.local` 设置 `NEXT_PUBLIC_API_URL`。复用 `uv.lock` 和 `web/bun.lock`，依赖变更时同步更新。

## 编码风格与命名

Python 使用 4 空格缩进，模块和函数用 `snake_case`，类用 `PascalCase`。TS/TSX 通常使用 2 空格；编辑时沿用当前文件的缩进和引号。组件用 `PascalCase`，文件用 `kebab-case`，函数和变量用 `camelCase`，内部导入可用 `@/` 别名。

新增前端代码使用 TypeScript 和 ES modules；外部数据先以 `unknown` 接收并校验。复用现有 UI 组件及样式。前端使用 Next.js/TypeScript ESLint 配置；当前没有统一格式化脚本，避免无关格式重写。

## 测试要求

后端单元测试以 `unittest` 为主，命名为 `test/test_*.py`。Windows 下优先在根目录运行 `.venv\Scripts\python.exe scripts\run_model_catalog_tests.py`；该入口在临时副本使用示例配置、屏蔽出站网络，日志写入 `logs/model-catalog-tests.log`，可追加指定的 unittest 名称。

前端测试使用 `node:test` 和 `node:assert/strict`，命名为 `*.test.ts` 或 `*.test.tsx`，在 `web/` 运行 `bun test src`。当前未设置覆盖率门槛；行为变更应覆盖成功、失败及边界情况。

`test/` 中也有真实服务与上游集成脚本；执行前检查凭据、存储和副作用，避免直接全量发现测试。

## 提交与 Pull Request

本地仓库尚无提交历史，暂无法归纳既有约定；建议使用 `feat: ...`、`fix: ...`、`docs: ...`，每次提交聚焦一个目的，例如 `fix: preserve model catalog on upstream failure` 或 `docs: add repository contributor guide`。PR 描述应说明问题、实际改动、验证命令及结果，关联相关 issue；UI 改动附截图，配置与 API 变更同步更新 `docs/`。

保留无关修改和已有测试。交付说明区分本地修改、提交、推送与部署，并列出未完成项。

## 配置与安全

首次运行才将 `config.example.json` 复制为 `config.json`，并自定义管理员密钥或设置 `MEDIA2API_AUTH_KEY`。保留已有配置、环境文件和 `data/`；禁止提交或记录真实令牌、账号密码与存储凭据。测试使用合成数据和隔离存储。Docker 工作流会在推送 `v*` 标签时发布镜像，发布和生产操作需已有授权。
