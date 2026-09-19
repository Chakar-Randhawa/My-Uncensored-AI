"use client";

import { useEffect, useRef, useState } from "react";
import { MessageBubble } from "./MessageBubble";
import { useSSEChat } from "./useSSEChat";

interface ChatInterfaceProps {
  conversationId?: string;
}

export function ChatInterface({ conversationId }: ChatInterfaceProps) {
  const { messages, sendMessage, stop, isStreaming, error } = useSSEChat(conversationId);
  const [input, setInput] = useState("");
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = input.trim();
    if (!trimmed || isStreaming) return;
    setInput("");
    void sendMessage(trimmed);
  };

  return (
    <div
      data-theme={theme}
      className="flex h-screen flex-col bg-background text-foreground"
    >
      <header className="flex items-center justify-between border-b border-border px-5 py-3">
        <div className="flex items-center gap-2">
          <div className="h-2 w-2 rounded-full bg-accent" />
          <span className="text-sm font-medium">AI Router</span>
        </div>
        <button
          type="button"
          onClick={() => setTheme((t) => (t === "light" ? "dark" : "light"))}
          className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-surface"
        >
          {theme === "light" ? "Dark" : "Light"}
        </button>
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
            {messages.map((message) => (
              <MessageBubble key={message.id} message={message} theme={theme} />
            ))}
          </div>
        )}
      </div>

      {error && (
        <div className="mx-auto w-full max-w-3xl px-4">
          <div className="mb-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        </div>
      )}

      <form onSubmit={handleSubmit} className="border-t border-border px-4 py-4">
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSubmit(e);
              }
            }}
            placeholder="Message the router…"
            rows={1}
            className="flex-1 resize-none rounded-md border border-border bg-surface px-3 py-2.5 text-[15px] outline-none placeholder:text-muted-foreground focus:border-accent"
          />
          {isStreaming ? (
            <button
              type="button"
              onClick={stop}
              className="rounded-md border border-border px-4 py-2.5 text-sm font-medium hover:bg-surface"
            >
              Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={!input.trim()}
              className="rounded-md bg-accent px-4 py-2.5 text-sm font-medium text-accent-foreground disabled:opacity-40"
            >
              Send
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
