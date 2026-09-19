"use client";

import { useCallback, useRef, useState } from "react";
import { createClient } from "@/lib/supabase/client";

export type ChatRole = "system" | "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  /** Set once the assistant message finishes streaming. */
  winningProvider?: string;
  isStreaming?: boolean;
}

interface TokenEventData {
  delta: string;
  conversation_id: string;
}

interface ErrorEventData {
  message: string;
}

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

/**
 * Consumes the FastAPI SSE endpoint chunk-by-chunk using `fetch` +
 * `ReadableStream` rather than the browser `EventSource` API, because
 * `EventSource` cannot send an `Authorization` header — and every request
 * here must carry the user's Supabase access token.
 */
export function useSSEChat(conversationId?: string) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  const stop = useCallback(() => {
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
    setIsStreaming(false);
  }, []);

  const sendMessage = useCallback(
    async (content: string, systemPrompt?: string) => {
      setError(null);

      const userMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content,
      };
      const assistantMessageId = crypto.randomUUID();

      setMessages((prev) => [
        ...prev,
        userMessage,
        { id: assistantMessageId, role: "assistant", content: "", isStreaming: true },
      ]);

      const supabase = createClient();
      const {
        data: { session },
      } = await supabase.auth.getSession();

      if (!session?.access_token) {
        setError("Your session expired — please sign in again.");
        setMessages((prev) => prev.filter((m) => m.id !== assistantMessageId));
        return;
      }

      const controller = new AbortController();
      abortControllerRef.current = controller;
      setIsStreaming(true);

      try {
        const response = await fetch(`${BACKEND_URL}/v1/chat/stream`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${session.access_token}`,
          },
          body: JSON.stringify({
            conversation_id: conversationId,
            messages: [{ role: "user", content }],
            system_prompt: systemPrompt || undefined,
          }),
          signal: controller.signal,
        });

        if (!response.ok || !response.body) {
          throw new Error(`Backend returned ${response.status}`);
        }

        await consumeSSEStream(response.body, {
          onToken: (data: TokenEventData) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId
                  ? { ...m, content: m.content + data.delta }
                  : m,
              ),
            );
          },
          onDone: () => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId ? { ...m, isStreaming: false } : m,
              ),
            );
          },
          onError: (data: ErrorEventData) => {
            setError(data.message);
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId ? { ...m, isStreaming: false } : m,
              ),
            );
          },
        });
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          setError(err instanceof Error ? err.message : "Something went wrong.");
        }
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMessageId ? { ...m, isStreaming: false } : m,
          ),
        );
      } finally {
        setIsStreaming(false);
        abortControllerRef.current = null;
      }
    },
    [conversationId],
  );

  return { messages, sendMessage, stop, isStreaming, error };
}

/**
 * Manual SSE frame parser for a `ReadableStream<Uint8Array>` body. Handles
 * `event:`/`data:` lines and frames split across chunk boundaries — `fetch`
 * gives no guarantee that one network chunk equals one SSE event.
 */
async function consumeSSEStream(
  body: ReadableStream<Uint8Array>,
  handlers: {
    onToken: (data: TokenEventData) => void;
    onDone: (data: { finish_reason: string }) => void;
    onError: (data: ErrorEventData) => void;
  },
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        const event = parseSSEFrame(frame);
        if (!event) continue;

        switch (event.event) {
          case "token":
            handlers.onToken(JSON.parse(event.data) as TokenEventData);
            break;
          case "done":
            handlers.onDone(JSON.parse(event.data) as { finish_reason: string });
            break;
          case "error":
            handlers.onError(JSON.parse(event.data) as ErrorEventData);
            break;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSSEFrame(frame: string): { event: string; data: string } | null {
  let eventName = "message";
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      eventName = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    }
  }

  if (dataLines.length === 0) return null;
  return { event: eventName, data: dataLines.join("\n") };
}
