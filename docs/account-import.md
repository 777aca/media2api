# 账号文本导入

在「号池管理 → 导入 → 导入 Access Token」中粘贴账号文本，或点击「选择 TXT」读取文件。读取文件后先检查识别数量和错误提示，再点击「导入账号」提交。

## 支持的格式

一行一个 Access Token，与原有导入方式兼容：

```text
ACCESS_TOKEN_1
ACCESS_TOKEN_2
```

也支持每行四段，以 `----` 分隔：

```text
user@example.com----PASSWORD----TOTP_SECRET----ACCESS_TOKEN
卡密 1: user@example.com----PASSWORD----TOTP_SECRET----ACCESS_TOKEN
账号 2： other@example.com----PASSWORD----TOTP_SECRET----OTHER_ACCESS_TOKEN
```

以上均为占位示例，使用时替换为实际账号字段。完整记录与逐行 Token 可以混合，支持空行、UTF-8 BOM 和常见换行符。

## 字段与校验

| 段 | 保存字段 | 说明 |
| --- | --- | --- |
| 1 | `email` | 自动去掉「卡密 N:」「账号 N:」「账户 N:」前缀 |
| 2 | `password` | 账号密码 |
| 3 | `totp_secret` | 二步验证密钥，仅保存；现有重新登录流程不会自动使用该字段 |
| 4 | `access_token` | 用于账号信息刷新与后续请求的 Access Token |

文本导入来源为 `web`。按 Access Token 去重；纯 Token 与完整记录重复时保留完整记录的附加字段。完整记录重复时采用最后一条记录。

格式错误会显示行号，不回显凭据。存在错误时不能提交，需要在输入框修正；正常导入仍沿用已有的账号信息与额度刷新流程。解析只检查输入格式，不代表已验证 Token 在线有效。
