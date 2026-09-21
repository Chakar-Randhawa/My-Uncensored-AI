"use client";

import { useCallback, useRef, useState } from "react";
import { createClient } from "@/lib/supabase/client";
import { createConversation } from "@/lib/conversations";
import type { ChatMessage, ProviderId } from "@/lib/types";
import { useToast } from "@/lib/useToast";

interface TokenEventData {
  delta: string;
  conversation_id: string;
  cached?: boolean;
}
interface DoneEventData {
  finish_reason: string;
  provider?: string;
}
interface ErrorEventData {
  message: string;
}

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

interface UseSSEChatOptions {
  conversationId?: string;
  initialMessages?: ChatMessage[];
  onConversationCreated?: (id: string) => void;
}

export function useSSEChat({
  conversationId: initialConversationId,
  initialMessages = [],
  onConversationCreated,
}: UseSSEChatOptions) {
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [isStreaming, setIsStreaming] = useState(false);
  const [providerPriority, setProviderPriority] = useState<ProviderId[] | null>(null);
  const conversationIdRef = useRef<string | undefined>(initialConversationId);
  const abortControllerRef = useRef<AbortController | null>(null);
  const { showToast } = useToast();

  const stop = useCallback(() => {
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
    setIsStreaming(false);
    setMessages((prev) => prev.map((m) => ({ ...m, isStreaming: false, isRetrying: false })));
  }, []);

  /**
   * Core streaming call. `historyForRequest` is the FULL message list to
   * send — this is the actual fix for "the model has no memory": every
   * previous turn in the conversation is sent on every request, not just
   * the newest message. `assistantMessageId` is the placeholder bubble
   * already inserted into state that this call will fill in as tokens
   * arrive.
   */
  const streamCompletion = useCallback(
    async (
      historyForRequest: ChatMessage[],
      assistantMessageId: string,
      options: { useCache?: boolean } = {},
    ) => {
      const supabase = createClient();
      const {
        data: { session },
      } = await supabase.auth.getSession();

      if (!session?.access_token) {
        showToast("Your session expired — please sign in again.");
        setMessages((prev) => prev.filter((m) => m.id !== assistantMessageId));
        return;
      }

      if (!conversationIdRef.current) {
        try {
          const created = await createConversation(
            historyForRequest[historyForRequest.length - 1]?.content.slice(0, 80) ||
              "New conversation",
          );
          conversationIdRef.current = created.id;
          onConversationCreated?.(created.id);
        } catch {
          // Non-fatal — the backend will still create the row via its own
          // `ensure_conversation` upsert once the reply lands; the sidebar
          // just won't show it until then.
        }
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
            conversation_id: conversationIdRef.current,
            messages: historyForRequest.map((m) => ({
              role: m.role,
              content: m.content,
              images: m.images ?? [],
            })),
            provider_priority: providerPriority,
            use_cache: options.useCache ?? true,
          }),
          signal: controller.signal,
        });

        if (!response.ok || !response.body) {
          throw new Error(`Backend returned ${response.status}`);
        }

        await consumeSSEStream(response.body, {
          onToken: (data) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId
                  ? {
                      ...m,
                      content: m.content + data.delta,
                      cached: data.cached ?? m.cached,
                      isRetrying: false,
                    }
                  : m,
              ),
            );
          },
          onRestart: () => {
            // The winning provider died mid-stream; the backend is
            // retrying with a different one from scratch. Clear the
            // partial text so we don't concatenate two different models'
            // half-answers together.
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId ? { ...m, content: "", isRetrying: true } : m,
              ),
            );
          },
          onDone: (data) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId
                  ? { ...m, isStreaming: false, isRetrying: false, winningProvider: data.provider ?? m.winningProvider }
                  : m,
              ),
            );
          },
          onError: (data) => {
            showToast(data.message);
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId ? { ...m, isStreaming: false, isRetrying: false } : m,
              ),
            );
          },
        });
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          showToast(err instanceof Error ? err.message : "Something went wrong.");
        }
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMessageId ? { ...m, isStreaming: false, isRetrying: false } : m,
          ),
        );
      } finally {
        setIsStreaming(false);
        abortControllerRef.current = null;
      }
    },
    [providerPriority, showToast, onConversationCreated],
  );

  const sendMessage = useCallback(
    async (content: string, images: string[] = []) => {
      const userMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content,
        images,
        createdAt: new Date().toISOString(),
      };
      const assistantMessageId = crypto.randomUUID();

      let fullHistory: ChatMessage[] = [];
      setMessages((prev) => {
        fullHistory = [...prev, userMessage];
        return [
          ...fullHistory,
          { id: assistantMessageId, role: "assistant", content: "", isStreaming: true },
        ];
      });

      // setMessages' updater runs synchronously in React 18/19 for this
      // call pattern, so fullHistory is populated before we read it below.
      await streamCompletion(fullHistory, assistantMessageId);
    },
    [streamCompletion],
  );

  /** Drop the last assistant reply and re-ask the same question, bypassing the cache. */
  const regenerate = useCallback(async () => {
    let historyForRequest: ChatMessage[] = [];
    let assistantMessageId = "";

    setMessages((prev) => {
      const lastAssistantIndex = [...prev].reverse().findIndex((m) => m.role === "assistant");
      if (lastAssistantIndex === -1) return prev;
      const cutIndex = prev.length - 1 - lastAssistantIndex;
      historyForRequest = prev.slice(0, cutIndex);
      assistantMessageId = crypto.randomUUID();
      return [
        ...historyForRequest,
        { id: assistantMessageId, role: "assistant", content: "", isStreaming: true },
      ];
    });

    if (historyForRequest.length > 0) {
      await streamCompletion(historyForRequest, assistantMessageId, { useCache: false });
    }
  }, [streamCompletion]);

  /** Edit a past user message and resend, discarding every turn after it. */
  const editAndResend = useCallback(
    async (messageId: string, newContent: string) => {
      let historyForRequest: ChatMessage[] = [];
      let assistantMessageId = "";

      setMessages((prev) => {
        const editIndex = prev.findIndex((m) => m.id === messageId);
        if (editIndex === -1) return prev;
        const edited: ChatMessage = { ...prev[editIndex], content: newContent };
        historyForRequest = [...prev.slice(0, editIndex), edited];
        assistantMessageId = crypto.randomUUID();
        return [
          ...historyForRequest,
          { id: assistantMessageId, role: "assistant", content: "", isStreaming: true },
        ];
      });

      if (historyForRequest.length > 0) {
        await streamCompletion(historyForRequest, assistantMessageId);
      }
    },
    [streamCompletion],
  );

  return {
    messages,
    sendMessage,
    regenerate,
    editAndResend,
    stop,
    isStreaming,
    providerPriority,
    setProviderPriority,
    conversationId: conversationIdRef.current,
  };
}

async function consumeSSEStream(
  body: ReadableStream<Uint8Array>,
  handlers: {
    onToken: (data: TokenEventData) => void;
    onRestart: () => void;
    onDone: (data: DoneEventData) => void;
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
          case "restart":
            handlers.onRestart();
            break;
          case "done":
            handlers.onDone(JSON.parse(event.data) as DoneEventData);
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
