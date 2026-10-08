export type Model = {
  id: string;
  object: string;
  created: number;
  owned_by: string;
  permission: unknown[];
  root: string;
  parent: string | null;
};

export type CatalogModel = Model & {
  source: "official" | "compatibility";
  channels?: ModelChannel[];
  display_name?: string;
  entry_kind?: "image_api" | "web_image";
};

export type ModelChannel = "web" | "codex";

export type ModelCatalogSync = {
  status: "success" | "partial" | "failed";
  last_attempt_at: string | null;
  last_success_at: string | null;
  source_count: number;
  successful_sources: number;
  errors: Array<{
    account_id: string | null;
    source: "account" | "anonymous";
    code: string;
    channel?: ModelChannel;
  }>;
};

export type ModelCatalogResponse = {
  object: "list";
  data: CatalogModel[];
  sync: ModelCatalogSync;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isTimestamp(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && Number.isFinite(Date.parse(value)));
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isModelChannel(value: unknown): value is ModelChannel {
  return value === "web" || value === "codex";
}

export function parseModelCatalogResponse(value: unknown): ModelCatalogResponse {
  const invalid = () => new Error("模型目录响应格式不正确，请稍后重新刷新");
  if (!isRecord(value) || value.object !== "list" || !Array.isArray(value.data) || !isRecord(value.sync)) {
    throw invalid();
  }
  const models: CatalogModel[] = value.data.map((item: unknown) => {
    if (
      !isRecord(item) || typeof item.id !== "string" || !item.id.trim() ||
      typeof item.object !== "string" || typeof item.created !== "number" || !Number.isFinite(item.created) ||
      typeof item.owned_by !== "string" || !Array.isArray(item.permission) || typeof item.root !== "string" ||
      (item.parent !== null && typeof item.parent !== "string") ||
      (item.source !== "official" && item.source !== "compatibility") ||
      (item.display_name !== undefined && typeof item.display_name !== "string") ||
      (item.entry_kind !== undefined && item.entry_kind !== "image_api" && item.entry_kind !== "web_image") ||
      (item.channels !== undefined && (!Array.isArray(item.channels) || !item.channels.every(isModelChannel)))
    ) {
      throw invalid();
    }
    return {
      id: item.id,
      object: item.object,
      created: item.created,
      owned_by: item.owned_by,
      permission: item.permission,
      root: item.root,
      parent: item.parent,
      source: item.source,
      ...(typeof item.display_name === "string" ? { display_name: item.display_name } : {}),
      ...(item.entry_kind === "image_api" || item.entry_kind === "web_image" ? { entry_kind: item.entry_kind } : {}),
      ...(Array.isArray(item.channels) ? { channels: [...new Set(item.channels.filter(isModelChannel))] } : {}),
    };
  });
  const sync = value.sync;
  if (
    (sync.status !== "success" && sync.status !== "partial" && sync.status !== "failed") ||
    !isTimestamp(sync.last_attempt_at) || !isTimestamp(sync.last_success_at) ||
    !isCount(sync.source_count) || !isCount(sync.successful_sources) ||
    sync.successful_sources > sync.source_count || !Array.isArray(sync.errors)
  ) {
    throw invalid();
  }
  const errors: ModelCatalogSync["errors"] = sync.errors.map((item: unknown) => {
    if (
      !isRecord(item) || (item.account_id !== null && typeof item.account_id !== "string") ||
      (item.source !== "account" && item.source !== "anonymous") || typeof item.code !== "string" || !/^[a-z][a-z0-9_]{0,63}$/.test(item.code) ||
      (item.channel !== undefined && !isModelChannel(item.channel))
    ) {
      throw invalid();
    }
    return { account_id: item.account_id, source: item.source, code: item.code,
      ...(isModelChannel(item.channel) ? { channel: item.channel } : {}) };
  });
  // 官方记录优先；不改变官方 ID，只合并重复项。
  const uniqueModels = new Map<string, CatalogModel>();
  for (const model of models) {
    const existing = uniqueModels.get(model.id);
    if (existing?.source === "official" && model.source === "official") {
      uniqueModels.set(model.id, { ...existing, ...model,
        channels: [...new Set([...(existing.channels ?? []), ...(model.channels ?? [])])] });
    } else if (!existing || model.source === "official") {
      uniqueModels.set(model.id, model);
    }
  }
  return {
    object: "list",
    data: [...uniqueModels.values()],
    sync: {
      status: sync.status,
      last_attempt_at: sync.last_attempt_at,
      last_success_at: sync.last_success_at,
      source_count: sync.source_count,
      successful_sources: sync.successful_sources,
      errors,
    },
  };
}
