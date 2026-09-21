import { notFound, redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { ChatInterface } from "@/components/chat/ChatInterface";
import type { ChatMessage } from "@/lib/types";

interface ConversationPageProps {
  params: Promise<{ conversationId: string }>;
}

export default async function ConversationPage({ params }: ConversationPageProps) {
  const { conversationId } = await params;
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    redirect(`/auth/sign-in?next=/chat/${conversationId}`);
  }

  // RLS (`auth.uid() = user_id`) means this query can only ever return a
  // conversation that belongs to the signed-in user — a stranger's
  // conversation id here just returns no rows rather than leaking data.
  const { data: conversation } = await supabase
    .from("conversations")
    .select("id")
    .eq("id", conversationId)
    .single();

  if (!conversation) {
    notFound();
  }

  const { data: messageRows } = await supabase
    .from("messages")
    .select("id, role, content, winning_provider, created_at")
    .eq("conversation_id", conversationId)
    .order("created_at", { ascending: true });

  const initialMessages: ChatMessage[] = (messageRows ?? []).map((row) => ({
    id: row.id,
    role: row.role,
    content: row.content,
    winningProvider: row.winning_provider ?? undefined,
    createdAt: row.created_at,
  }));

  return <ChatInterface conversationId={conversationId} initialMessages={initialMessages} />;
}
