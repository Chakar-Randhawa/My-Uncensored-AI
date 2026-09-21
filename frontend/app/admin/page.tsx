"use client";

import { useEffect, useState } from "react";
import { createClient } from "@/lib/supabase/client";
import { useLocalStorage } from "@/lib/useLocalStorage";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

interface ProviderStat {
  wins: number;
  avg_latency_ms: number;
  total_tokens: number;
}

interface StatsResponse {
  sample_size: number;
  provider_stats: Record<string, ProviderStat>;
  circuit_breakers: Record<string, { state: string; consecutive_failures: number }>;
}

export default function AdminPage() {
  const [theme] = useLocalStorage<"light" | "dark">("ai-router-theme", "light");
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      const supabase = createClient();
      const {
        data: { session },
      } = await supabase.auth.getSession();

      if (!session?.access_token) {
        if (!cancelled) setError("Sign in to view the admin dashboard.");
        return;
      }

      try {
        const response = await fetch(`${BACKEND_URL}/v1/admin/stats`, {
          headers: { Authorization: `Bearer ${session.access_token}` },
        });
        if (!response.ok) throw new Error(`Backend returned ${response.status}`);
        const data = (await response.json()) as StatsResponse;
        if (!cancelled) setStats(data);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load stats.");
      }
    }

    void load();
    const interval = setInterval(load, 15_000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const maxWins = stats
    ? Math.max(1, ...Object.values(stats.provider_stats).map((s) => s.wins))
    : 1;

  return (
    <div data-theme={theme} className="min-h-screen bg-background p-8 text-foreground">
      <h1 className="mb-1 text-lg font-medium">Provider stats</h1>
      <p className="mb-6 text-sm text-muted-foreground">
        Last {stats?.sample_size ?? "…"} assistant replies · refreshes every 15s
      </p>

      {error && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      )}

      {stats && (
        <div className="grid gap-6 lg:grid-cols-2">
          <section>
            <h2 className="mb-3 text-sm font-medium text-muted-foreground">Win share &amp; latency</h2>
            <div className="flex flex-col gap-3">
              {Object.entries(stats.provider_stats)
                .sort(([, a], [, b]) => b.wins - a.wins)
                .map(([provider, s]) => (
                  <div key={provider} className="rounded-md border border-border p-3">
                    <div className="mb-1.5 flex items-baseline justify-between">
                      <span className="text-sm font-medium">{provider}</span>
                      <span className="text-xs text-muted-foreground">
                        {s.wins} wins · avg {s.avg_latency_ms}ms TTFT · {s.total_tokens} tokens
                      </span>
                    </div>
                    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-accent"
                        style={{ width: `${(s.wins / maxWins) * 100}%` }}
                      />
                    </div>
                  </div>
                ))}
              {Object.keys(stats.provider_stats).length === 0 && (
                <p className="text-sm text-muted-foreground">No completed requests yet.</p>
              )}
            </div>
          </section>

          <section>
            <h2 className="mb-3 text-sm font-medium text-muted-foreground">Circuit breakers</h2>
            <div className="flex flex-col gap-2">
              {Object.entries(stats.circuit_breakers).map(([provider, breaker]) => (
                <div
                  key={provider}
                  className="flex items-center justify-between rounded-md border border-border px-3 py-2 text-sm"
                >
                  <span>{provider}</span>
                  <span className="flex items-center gap-2 text-xs">
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${
                        breaker.state === "closed"
                          ? "bg-emerald-500"
                          : breaker.state === "half_open"
                            ? "bg-amber-500"
                            : "bg-destructive"
                      }`}
                    />
                    {breaker.state} · {breaker.consecutive_failures} failures
                  </span>
                </div>
              ))}
              {Object.keys(stats.circuit_breakers).length === 0 && (
                <p className="text-sm text-muted-foreground">All breakers closed — nothing to show.</p>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
