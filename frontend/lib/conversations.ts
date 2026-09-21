import { createClient } from "@/lib/supabase/client";
import type { ChatMessage, Conversation } from "@/lib/types";

/**
 * All reads/writes here go through the browser's Supabase client (anon
 * key), so RLS (`auth.uid() = user_id`) is what actually scopes every
 * query to the signed-in user — there is no separate authorization check
 * needed in this file.
 */

export async function listConversations(): Promise<Conversation[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from("conversations")
    .select("id, title, updated_at, is_archived")
    .eq("is_archived", false)
    .order("updated_at", { ascending: false })
    .limit(100);

  if (error) throw error;

  return (data ?? []).map((row) => ({
    id: row.id,
    title: row.title,
    updatedAt: row.updated_at,
    isArchived: row.is_archived,
  }));
}

export async function loadConversationMessages(conversationId: string): Promise<ChatMessage[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from("messages")
    .select("id, role, content, winning_provider, created_at")
    .eq("conversation_id", conversationId)
    .order("created_at", { ascending: true });

  if (error) throw error;

  return (data ?? []).map((row) => ({
    id: row.id,
    role: row.role,
    content: row.content,
    winningProvider: row.winning_provider ?? undefined,
    createdAt: row.created_at,
  }));
}

/**
 * Creates the conversation row up front (rather than waiting for the
 * backend's `ensure_conversation` upsert after the first reply) so it
 * appears in the sidebar immediately, even before the first token streams
 * back.
 */
export async function createConversation(title = "New conversation"): Promise<Conversation> {
  const supabase = createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) throw new Error("Not signed in.");

  const { data, error } = await supabase
    .from("conversations")
    .insert({ user_id: user.id, title })
    .select("id, title, updated_at, is_archived")
    .single();

  if (error) throw error;

  return {
    id: data.id,
    title: data.title,
    updatedAt: data.updated_at,
    isArchived: data.is_archived,
  };
}

export async function renameConversation(conversationId: string, title: string): Promise<void> {
  const supabase = createClient();
  const { error } = await supabase
    .from("conversations")
    .update({ title })
    .eq("id", conversationId);
  if (error) throw error;
}

export async function deleteConversation(conversationId: string): Promise<void> {
  const supabase = createClient();
  const { error } = await supabase.from("conversations").delete().eq("id", conversationId);
  if (error) throw error;
}
