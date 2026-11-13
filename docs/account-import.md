# 账号文本导入

在「号池管理 → 导入 → 导入 Access Token」中粘贴账号文本，或点击「选择 TXT」读取文件。读取文件后先检查识别数量和错误提示，再点击「导入账号」提交。

## 支持的格式

一行一个 Access Token，与原有导入方式兼容：

```text
ACCESS_TOKEN_1
ACCESS_TOKEN_2
```

支持每行三段或四段，以 `----` 分隔。三段式会使用邮箱、密码和二步验证密钥自动登录并获取 Access Token：

```text
卡密导出

user@example.com----PASSWORD----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP
```

已有 Access Token 时可直接使用四段式：

```text
user@example.com----PASSWORD----TOTP_SECRET----ACCESS_TOKEN
卡密 1: user@example.com----PASSWORD----TOTP_SECRET----ACCESS_TOKEN
账号 2： other@example.com----PASSWORD----TOTP_SECRET----OTHER_ACCESS_TOKEN
```

以上均为占位示例，使用时替换为实际账号字段。三段式、四段式与逐行 Token 可以混合，支持「卡密导出」标题、空行、UTF-8 BOM 和常见换行符。

## 字段与校验

| 段 | 保存字段 | 说明 |
| --- | --- | --- |
| 1 | `email` | 自动去掉「卡密 N:」「账号 N:」「账户 N:」前缀 |
| 2 | `password` | 账号密码 |
| 3 | `totp_secret` | 二步验证密钥；三段式须为有效 Base32，至少 16 位。自动登录遇到 TOTP 挑战时本地生成 6 位动态码，密钥本身不会发往上游 |
| 4（可选） | `access_token` | 已有的 Access Token；省略整段时自动登录，四段式不能将此段留空 |

文本导入来源为 `web`。四段式按 Access Token 去重；纯 Token 与完整记录重复时保留完整记录的附加字段。三段式在批内按邮箱去重（忽略大小写），采用最后一条记录。重新导入同邮箱的 Web 账号时更新真实 Token，保留号池内部标识和调度设置。

格式错误会显示行号，不回显凭据。存在错误时不能提交，需要在输入框修正。三段式最多同时登录 3 个账号，登录成功后保存密码、二步验证密钥及上游返回的 Token，并沿用账号信息和额度刷新流程。

登录部分失败时保留成功账号并显示失败项在三段式列表中的序号，输入框会保留，便于修正重试。登录失败的账号不会写入号池；错误信息不会包含密码、密钥、Token 或上游原始响应。邮箱验证码、额外手机验证或其他交互式检查仍需通过浏览器 OAuth 完成，TOTP 不能代替邮箱验证码。服务器时钟需准确。

## API

`POST /api/accounts` 保持原有 `tokens` 和 `accounts` 参数，管理员鉴权不变。`accounts` 现在也支持没有 `access_token`、但完整提供 `email`、`password`、`totp_secret` 的对象：

```json
{
  "accounts": [{
    "email": "user@example.com",
    "password": "PASSWORD",
    "totp_secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
  }]
}
```

整批凭据先校验，格式无效返回 HTTP 400，不启动登录或写入。登录失败按批处理返回 HTTP 200，`errors` 中增加 `{ "account_index": 1, "error": "第 1 条三段式账号：密码错误" }`；全部失败时 `added`、`refreshed` 均为 0，原有账号仍通过 `items` 返回。前端需同时检查 `errors`，不能仅依据 HTTP 状态判断导入成功。不要将此接口的请求体写入日志。

TOTP 验证分支参考公开实现中的 [`mfa/verify` 请求](https://github.com/Regert888/gpt-auto-register/blob/main/auth_flow.py)，并在隔离测试中覆盖密码验证、TOTP、授权重定向和 Token 兑换。这不是上游公开稳定接口；真实环境可能因风控或登录协议变化而失败。
