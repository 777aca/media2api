export type EditableFileTask = {
  id: string;
  taskId?: string;
  status: "queued" | "running" | "success" | "error" | string;
  kind: "ppt" | "psd" | string;
  created_at?: string;
  updated_at?: string;
  elapsed_seconds?: number;
  polled_at?: number;
  prompt_preview?: string;
  error?: string;
  result?: {
    conversation_id?: string;
    primary_url?: string;
    zip_url?: string;
  };
};
