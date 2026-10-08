import type { AccountImportPayload } from "@/lib/api";

export type AccountTextImportError = {
  line: number;
  message: string;
};

export type ParsedAccountImportText = {
  tokens: string[];
  accounts: AccountImportPayload[];
  errors: AccountTextImportError[];
};

export function parseAccountImportText(value: string): ParsedAccountImportText {
  const tokens = new Set<string>();
  const accounts = new Map<string, AccountImportPayload>();
  const errors: AccountTextImportError[] = [];

  for (const [index, rawLine] of value.replace(/^\uFEFF/, "").split(/\r\n|\n|\r/).entries()) {
    const line = rawLine.trim();
    if (!line) continue;

    const fail = (message: string) => errors.push({ line: index + 1, message });
    if (!line.includes("----")) {
      if (/\s/.test(line)) {
        fail("Token 不能包含空白字符");
      } else {
        tokens.add(line);
      }
      continue;
    }

    const fields = line.split("----").map((field) => field.trim());
    if (fields.length !== 4) {
      fail("应为邮箱、密码、二步验证密钥、Access Token 共 4 段，使用 ---- 分隔");
      continue;
    }

    const [emailField, password, totpSecret, accessToken] = fields;
    const email = emailField.replace(/^(?:卡密|账号|账户)\s*\d*\s*[:：]\s*/u, "");
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      fail("邮箱格式不正确");
      continue;
    }
    if (!password || !totpSecret || !accessToken) {
      fail("密码、二步验证密钥和 Access Token 均不能为空");
      continue;
    }
    if (/\s/.test(accessToken)) {
      fail("Access Token 不能包含空白字符");
      continue;
    }

    accounts.set(accessToken, {
      access_token: accessToken,
      email,
      password,
      totp_secret: totpSecret,
      source_type: "web",
    });
  }

  return {
    tokens: [...tokens].filter((token) => !accounts.has(token)),
    accounts: [...accounts.values()],
    errors,
  };
}
