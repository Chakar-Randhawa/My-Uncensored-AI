"use client";

import { useEffect, useState } from "react";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";
const PING_INTERVAL_MS = 30_000;

export type ConnectionState = "online" | "offline" | "backend-unreachable";

export function useConnectionStatus(): ConnectionState {
  const [browserOnline, setBrowserOnline] = useState(true);
  const [backendReachable, setBackendReachable] = useState(true);

  useEffect(() => {
    setBrowserOnline(navigator.onLine);
    const handleOnline = () => setBrowserOnline(true);
    const handleOffline = () => setBrowserOnline(false);
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const ping = async () => {
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 5000);
        const response = await fetch(`${BACKEND_URL}/health`, { signal: controller.signal });
        clearTimeout(timeout);
        if (!cancelled) setBackendReachable(response.ok);
      } catch {
        if (!cancelled) setBackendReachable(false);
      }
    };

    void ping();
    const interval = setInterval(ping, PING_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  if (!browserOnline) return "offline";
  if (!backendReachable) return "backend-unreachable";
  return "online";
}
