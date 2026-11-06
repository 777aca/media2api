import type { Account, AccountStatus } from "./api";
import { items, record } from "./native-media";

export type GrokAccount = {
  id: string;
  name: string;
  email: string;
  enabled: boolean;
  status: string;
  tier: string;
  cooldownUntil: number;
  createdAt: number;
  requestInflight: number;
  quotas: Record<string, { remaining: number | null }>;
};

export function parseGrokAccounts(value: unknown): GrokAccount[] {
  return items(value).flatMap(a => {
    if (typeof a.id !== "string" || !a.id) return [];
    return [{
      id: a.id,
      name: typeof a.name === "string" ? a.name : "Grok 账号",
      email: typeof a.email === "string" ? a.email : "",
      enabled: a.enabled === true,
      status: typeof a.status === "string" ? a.status : "unknown",
      tier: typeof a.tier === "string" ? a.tier : "auto",
      cooldownUntil: typeof a.cooldown_until === "number" ? a.cooldown_until : 0,
      createdAt: typeof a.created_at === "number" ? a.created_at : 0,
      requestInflight: typeof a.request_inflight === "number" && Number.isSafeInteger(a.request_inflight) && a.request_inflight >= 0 ? a.request_inflight : 0,
      quotas: Object.fromEntries(Object.entries(record(a.quotas)).map(([key, value]) => {
        const remaining = record(value).remaining;
        return [key, { remaining: typeof remaining === "number" && Number.isFinite(remaining) ? remaining : null }];
      })),
    }];
  });
}

export type PoolChannel = "chatgpt" | "codex" | "grok";
export const channelLabels: Record<PoolChannel, string> = { chatgpt: "ChatGPT Web", codex: "Codex", grok: "Grok Web" };

export function grokStatus(a: GrokAccount, now: number): AccountStatus {
  if (!a.enabled) return "禁用";
  if (a.status !== "active") return "异常";
  if (a.cooldownUntil * 1000 > now) return "限流";
  return "正常";
}

type PoolFields = { key: string; channel: PoolChannel; name: string; tier: string; status: AccountStatus };
export type PoolRow = PoolFields & (
  | { kind: "openai"; account: Account }
  | { kind: "grok"; account: GrokAccount }
);

export function poolRows(accounts: Account[], grok: GrokAccount[], now: number): PoolRow[] {
  return [
    ...accounts.map((a): PoolRow => ({
      key: `openai:${a.access_token}`, kind: "openai", account: a,
      channel: a.source_type?.trim().toLowerCase() === "codex" ? "codex" : "chatgpt",
      name: a.email || "", tier: a.type || "Free", status: a.status,
    })),
    ...grok.map((a): PoolRow => ({
      key: `grok:${a.id}`, kind: "grok", account: a, channel: "grok",
      name: `${a.name} ${a.email}`, tier: a.tier, status: grokStatus(a, now),
    })),
  ];
}

export function filterPoolRows(rows: PoolRow[], query: string, channel: string, tier: string, status: string): PoolRow[] {
  const search = query.trim().toLowerCase();
  return rows.filter(row =>
    (channel === "all" || row.channel === channel) &&
    (tier === "all" || row.tier === tier) &&
    (status === "all" || row.status === status) &&
    `${row.name} ${channelLabels[row.channel]} ${row.tier} ${row.kind === "grok" ? row.account.id : row.account.pool_account_id || ""}`.toLowerCase().includes(search),
  );
}

export function partitionPoolRows(rows: PoolRow[]) {
  return {
    tokens: rows.flatMap(row => row.kind === "openai" ? [row.account.access_token] : []),
    grokIds: rows.flatMap(row => row.kind === "grok" ? [row.account.id] : []),
  };
}
