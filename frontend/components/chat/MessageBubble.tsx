"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight, oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import type { ChatMessage } from "@/lib/types";

interface MessageBubbleProps {
  message: ChatMessage;
  theme: "light" | "dark";
  onRegenerate?: () => void;
  onEditAndResend?: (newContent: string) => void;
}

export function MessageBubble({ message, theme, onRegenerate, onEditAndResend }: MessageBubbleProps) {
  const isUser = message.role === "user";
  const [copied, setCopied] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API unavailable/blocked — silently do nothing rather
      // than throw over a non-critical convenience feature.
    }
  };

  const handleEditSubmit = () => {
    const trimmed = draft.trim();
    setIsEditing(false);
    if (trimmed && trimmed !== message.content) {
      onEditAndResend?.(trimmed);
    }
  };

  return (
    <div className={`group flex w-full gap-3 px-4 py-3 ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[720px] ${isUser ? "items-end" : "items-start"} flex flex-col gap-1`}>
        {message.images && message.images.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {message.images.map((src, i) => (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={i}
                src={src}
                alt="Attached"
                className="h-24 w-24 rounded-md border border-border object-cover"
              />
            ))}
          </div>
        )}

        {isEditing ? (
          <div className="flex w-full min-w-[280px] flex-col gap-2 rounded-md border border-accent bg-surface p-3">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={3}
              className="resize-none bg-transparent text-[15px] outline-none"
              autoFocus
            />
            <div className="flex justify-end gap-2 text-xs">
              <button
                onClick={() => {
                  setDraft(message.content);
                  setIsEditing(false);
                }}
                className="rounded px-2 py-1 text-muted-foreground hover:bg-muted"
              >
                Cancel
              </button>
              <button
                onClick={handleEditSubmit}
                className="rounded bg-accent px-2 py-1 font-medium text-accent-foreground"
              >
                Save &amp; resend
              </button>
            </div>
          </div>
        ) : (
          <div
            className={`rounded-md px-4 py-3 text-[15px] leading-relaxed ${
              isUser
                ? "bg-accent text-accent-foreground"
                : "bg-surface text-foreground border border-border"
            }`}
          >
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                code(props) {
                  const { children, className, ...rest } = props;
                  const match = /language-(\w+)/.exec(className ?? "");
                  const isInline = !match;

                  if (isInline) {
                    return (
                      <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[13px]" {...rest}>
                        {children}
                      </code>
                    );
                  }

                  return (
                    <SyntaxHighlighter
                      language={match?.[1]}
                      style={theme === "dark" ? oneDark : oneLight}
                      customStyle={{
                        margin: "0.5rem 0",
                        borderRadius: "6px",
                        fontSize: "13px",
                        border: "1px solid var(--border)",
                      }}
                      PreTag="div"
                    >
                      {String(children).replace(/\n$/, "")}
                    </SyntaxHighlighter>
                  );
                },
                p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
                ul: ({ children }) => <ul className="mb-2 list-disc pl-5 last:mb-0">{children}</ul>,
                ol: ({ children }) => <ol className="mb-2 list-decimal pl-5 last:mb-0">{children}</ol>,
                a: ({ children, href }) => (
                  <a
                    href={href}
                    target="_blank"
                    rel="noreferrer"
                    className="underline decoration-border underline-offset-2 hover:decoration-foreground"
                  >
                    {children}
                  </a>
                ),
              }}
            >
              {message.content}
            </ReactMarkdown>

            {message.isRetrying && (
              <p className="mt-1 text-xs text-muted-foreground">
                That provider disconnected — retrying with another one…
              </p>
            )}

            {message.isStreaming && message.content === "" && !message.isRetrying && (
              <span className="inline-flex items-center gap-1 text-muted-foreground">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current [animation-delay:150ms]" />
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current [animation-delay:300ms]" />
              </span>
            )}
          </div>
        )}

        {/* Meta row: timestamp, provider/cache badge, and hover actions. */}
        <div className="flex items-center gap-2 px-1 text-xs text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100">
          {message.createdAt && (
            <time dateTime={message.createdAt}>
              {new Date(message.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </time>
          )}

          {!isUser && !message.isStreaming && message.winningProvider && (
            <span>
              {message.winningProvider}
              {message.cached ? " · cached" : ""}
            </span>
          )}

          {message.content && (
            <button onClick={handleCopy} className="hover:text-foreground">
              {copied ? "Copied" : "Copy"}
            </button>
          )}

          {isUser && !isEditing && (
            <button onClick={() => setIsEditing(true)} className="hover:text-foreground">
              Edit
            </button>
          )}

          {!isUser && !message.isStreaming && onRegenerate && (
            <button onClick={onRegenerate} className="hover:text-foreground">
              Regenerate
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
