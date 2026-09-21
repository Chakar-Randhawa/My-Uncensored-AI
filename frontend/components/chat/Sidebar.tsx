"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  deleteConversation,
  listConversations,
  renameConversation,
} from "@/lib/conversations";
import type { Conversation } from "@/lib/types";
import { useToast } from "@/lib/useToast";

interface SidebarProps {
  activeConversationId?: string;
  refreshKey: number; // bump this to force a re-fetch (e.g. after a new chat's first message)
}

export function Sidebar({ activeConversationId, refreshKey }: SidebarProps) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const router = useRouter();
  const { showToast } = useToast();

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    listConversations()
      .then((data) => {
        if (!cancelled) setConversations(data);
      })
      .catch(() => {
        if (!cancelled) showToast("Couldn't load your conversation history.");
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm("Delete this conversation? This can't be undone.")) return;

    const previous = conversations;
    setConversations((prev) => prev.filter((c) => c.id !== id));
    try {
      await deleteConversation(id);
      if (id === activeConversationId) router.push("/chat");
    } catch {
      setConversations(previous);
      showToast("Couldn't delete that conversation.");
    }
  };

  const commitRename = async (id: string) => {
    const title = editValue.trim();
    setEditingId(null);
    if (!title) return;

    const previous = conversations;
    setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, title } : c)));
    try {
      await renameConversation(id, title);
    } catch {
      setConversations(previous);
      showToast("Couldn't rename that conversation.");
    }
  };

  return (
    <aside className="flex h-full w-64 flex-col border-r border-border bg-surface">
      <div className="p-3">
        <button
          onClick={() => router.push("/chat")}
          className="w-full rounded-md border border-border px-3 py-2 text-left text-sm font-medium hover:bg-muted"
        >
          + New chat
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-3">
        {isLoading ? (
          <div className="flex flex-col gap-1.5 px-1">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="h-8 animate-pulse rounded-md bg-muted" />
            ))}
          </div>
        ) : conversations.length === 0 ? (
          <p className="px-2 py-4 text-xs text-muted-foreground">No conversations yet.</p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {conversations.map((conv) => (
              <li key={conv.id}>
                {editingId === conv.id ? (
                  <input
                    autoFocus
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    onBlur={() => commitRename(conv.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitRename(conv.id);
                      if (e.key === "Escape") setEditingId(null);
                    }}
                    className="w-full rounded-md border border-accent bg-background px-2 py-1.5 text-sm outline-none"
                  />
                ) : (
                  <button
                    onClick={() => router.push(`/chat/${conv.id}`)}
                    onDoubleClick={() => {
                      setEditingId(conv.id);
                      setEditValue(conv.title);
                    }}
                    className={`group flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm ${
                      conv.id === activeConversationId
                        ? "bg-muted text-foreground"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground"
                    }`}
                  >
                    <span className="truncate">{conv.title}</span>
                    <span
                      onClick={(e) => handleDelete(conv.id, e)}
                      className="ml-2 shrink-0 opacity-0 hover:text-destructive group-hover:opacity-100"
                      title="Delete"
                    >
                      ✕
                    </span>
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
