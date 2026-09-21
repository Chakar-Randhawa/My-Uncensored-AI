"use client";

import { useEffect, useState } from "react";

/**
 * Like `useState`, but persisted to `localStorage`. Reads lazily on mount
 * (SSR-safe — falls back to `initialValue` on the server, then syncs from
 * storage once mounted, so there's no hydration mismatch).
 */
export function useLocalStorage<T>(key: string, initialValue: T) {
  const [value, setValue] = useState<T>(initialValue);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(key);
      if (stored !== null) setValue(JSON.parse(stored) as T);
    } catch {
      // Corrupt/blocked storage — silently keep the initial value.
    } finally {
      setHydrated(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    if (!hydrated) return; // don't overwrite storage with the initial value before we've read it
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Storage full/blocked — the app still works, just without persistence.
    }
  }, [key, value, hydrated]);

  return [value, setValue] as const;
}
