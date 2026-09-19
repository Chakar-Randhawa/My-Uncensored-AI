"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight, oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import type { ChatMessage } from "./useSSEChat";

interface MessageBubbleProps {
  message: ChatMessage;
  theme: "light" | "dark";
}

export function MessageBubble({ message, theme }: MessageBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div
      className={`flex w-full gap-3 px-4 py-3 ${isUser ? "justify-end" : "justify-start"}`}
    >
      <div
        className={`max-w-[720px] rounded-md px-4 py-3 text-[15px] leading-relaxed ${
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
                  <code
                    className="rounded bg-muted px-1.5 py-0.5 font-mono text-[13px]"
                    {...rest}
                  >
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
            ul: ({ children }) => (
              <ul className="mb-2 list-disc pl-5 last:mb-0">{children}</ul>
            ),
            ol: ({ children }) => (
              <ol className="mb-2 list-decimal pl-5 last:mb-0">{children}</ol>
            ),
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
          {message.content || (message.isStreaming ? "" : "")}
        </ReactMarkdown>

        {message.isStreaming && message.content === "" && (
          <span className="inline-flex items-center gap-1 text-muted-foreground">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current [animation-delay:150ms]" />
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current [animation-delay:300ms]" />
          </span>
        )}

        {!isUser && !message.isStreaming && message.winningProvider && (
          <div className="mt-2 text-xs text-muted-foreground">
            {message.winningProvider}
          </div>
        )}
      </div>
    </div>
  );
}
