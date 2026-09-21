"use client";

import { useConnectionStatus } from "@/lib/useConnectionStatus";

export function ConnectionStatus() {
  const status = useConnectionStatus();

  if (status === "online") return null; // silent when everything's fine — no need to clutter the header

  const label = status === "offline" ? "You're offline" : "Backend unreachable";

  return (
    <div className="flex items-center gap-1.5 rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-1 text-xs text-destructive">
      <span className="h-1.5 w-1.5 rounded-full bg-destructive" />
      {label}
    </div>
  );
}
