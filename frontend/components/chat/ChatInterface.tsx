"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { MessageBubble } from "./MessageBubble";
import { Sidebar } from "./Sidebar";
import { ProviderSelector } from "./ProviderSelector";
import { ConnectionStatus } from "./ConnectionStatus";
import { useSSEChat } from "./useSSEChat";
import { useLocalStorage } from "@/lib/useLocalStorage";
import { fileToDataUrl } from "@/lib/fileToDataUrl";
import { useToast } from "@/lib/useToast";
import type { ChatMessage } from "@/lib/types";

interface ChatInterfaceProps {
  conversationId?: string;
  initialMessages?: ChatMessage[];
}

const MAX_ATTACHMENTS = 4;

export function ChatInterface({ conversationId, initialMessages = [] }: ChatInterfaceProps) {
  const router = useRouter();
  const { showToast } = useToast();
  const [theme, setTheme] = useLocalStorage<"light" | "dark">("ai-router-theme", "light");
  const [sidebarRefreshKey, setSidebarRefreshKey] = useState(0);

  const {
    messages,
    sendMessage,
    regenerate,
    editAndResend,
    stop,
    isStreaming,
    providerPriority,
    setProviderPriority,
  } = useSSEChat({
    conversationId,
    initialMessages,
    onConversationCreated: (id) => {
      setSidebarRefreshKey((k) => k + 1);
      router.replace(`/chat/${id}`);
    },
  });

  const [input, setInput] = useState("");
  const [pendingImages, setPendingImages] = useState<string[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  // Auto-resize the composer textarea up to a max height, then scroll internally.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  // Keyboard shortcuts: Cmd/Ctrl+K -> new chat, Esc -> stop streaming.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        router.push("/chat");
      }
      if (e.key === "Escape" && isStreaming) {
        stop();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [router, isStreaming, stop]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = input.trim();
    if ((!trimmed && pendingImages.length === 0) || isStreaming) return;
    setInput("");
    const images = pendingImages;
    setPendingImages([]);
    void sendMessage(trimmed, images);
  };

  const handleFileSelect = async (files: FileList | null) => {
    if (!files) return;
    const remaining = MAX_ATTACHMENTS - pendingImages.length;
    if (remaining <= 0) {
      showToast(`You can attach up to ${MAX_ATTACHMENTS} images.`, "info");
      return;
    }

    for (const file of Array.from(files).slice(0, remaining)) {
      try {
        const dataUrl = await fileToDataUrl(file);
        setPendingImages((prev) => [...prev, dataUrl]);
      } catch (err) {
        showToast(err instanceof Error ? err.message : "Couldn't attach that file.");
      }
    }
  };

  const charCount = input.length;

  return (
    <div data-theme={theme} className="flex h-screen bg-background text-foreground">
      <Sidebar activeConversationId={conversationId} refreshKey={sidebarRefreshKey} />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <div className="h-2 w-2 rounded-full bg-accent" />
            <span className="text-sm font-medium">AI Router</span>
          </div>

          <div className="flex items-center gap-2">
            <ConnectionStatus />
            <ProviderSelector value={providerPriority} onChange={setProviderPriority} />
            <button
              type="button"
              onClick={() => setTheme(theme === "light" ? "dark" : "light")}
              className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-surface"
            >
              {theme === "light" ? "Dark" : "Light"}
            </button>
          </div>
        </header>

        <div ref={scrollRef} className="flex-1 overflow-y-auto">
          {messages.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <p className="text-sm text-muted-foreground">
                Ask anything — your request races across every configured provider.
              </p>
            </div>
          ) : (
            <div className="mx-auto flex max-w-3xl flex-col py-4">
              {messages.map((message, i) => {
                const isLastAssistant =
                  message.role === "assistant" && i === messages.length - 1 && !message.isStreaming;
                return (
                  <MessageBubble
                    key={message.id}
                    message={message}
                    theme={theme}
                    onRegenerate={isLastAssistant ? regenerate : undefined}
                    onEditAndResend={
                      message.role === "user"
                        ? (newContent) => editAndResend(message.id, newContent)
                        : undefined
                    }
                  />
                );
              })}
            </div>
          )}
        </div>

        <form onSubmit={handleSubmit} className="border-t border-border px-4 py-4">
          <div className="mx-auto flex max-w-3xl flex-col gap-2">
            {pendingImages.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {pendingImages.map((src, i) => (
                  <div key={i} className="relative">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={src} alt="Pending attachment" className="h-16 w-16 rounded-md border border-border object-cover" />
                    <button
                      type="button"
                      onClick={() => setPendingImages((prev) => prev.filter((_, idx) => idx !== i))}
                      className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-destructive text-xs text-white"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}

            <div className="flex items-end gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                multiple
                hidden
                onChange={(e) => {
                  void handleFileSelect(e.target.files);
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                title="Attach images"
                className="rounded-md border border-border px-3 py-2.5 text-sm text-muted-foreground hover:bg-surface"
              >
                📎
              </button>

              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleSubmit(e);
                  }
                }}
                placeholder="Message the router… (Shift+Enter for a new line)"
                rows={1}
                className="max-h-[200px] flex-1 resize-none overflow-y-auto rounded-md border border-border bg-surface px-3 py-2.5 text-[15px] outline-none placeholder:text-muted-foreground focus:border-accent"
              />

              {isStreaming ? (
                <button
                  type="button"
                  onClick={stop}
                  className="rounded-md border border-border px-4 py-2.5 text-sm font-medium hover:bg-surface"
                  title="Esc"
                >
                  Stop
                </button>
              ) : (
                <button
                  type="submit"
                  disabled={!input.trim() && pendingImages.length === 0}
                  className="rounded-md bg-accent px-4 py-2.5 text-sm font-medium text-accent-foreground disabled:opacity-40"
                >
                  Send
                </button>
              )}
            </div>

            <div className="flex justify-between px-1 text-[11px] text-muted-foreground">
              <span>⌘K new chat · Esc stop</span>
              {charCount > 0 && <span>{charCount} characters</span>}
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
