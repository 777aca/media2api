"use client";

import { useState, type ReactNode } from "react";

export function SettingsSection({ title, description, children }: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  const [hasOpened, setHasOpened] = useState(false);

  return (
    <details
      className="rounded-2xl border border-stone-200 bg-white/90"
      onToggle={(event) => { if (event.currentTarget.open) setHasOpened(true); }}
    >
      <summary className="cursor-pointer rounded-2xl p-5 focus-visible:outline-2 focus-visible:outline-offset-2">
        <span className="font-medium">{title}</span>
        <span className="mt-1 block text-sm text-stone-500">{description}</span>
      </summary>
      {hasOpened && <div className="space-y-4 border-t p-4 sm:p-5">{children}</div>}
    </details>
  );
}
