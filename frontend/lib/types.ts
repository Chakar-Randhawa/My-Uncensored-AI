export type ChatRole = "system" | "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  images?: string[];
  createdAt?: string;
  winningProvider?: string;
  cached?: boolean;
  isStreaming?: boolean;
  /** Set while a mid-stream failover retry is in flight for this message. */
  isRetrying?: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  updatedAt: string;
  isArchived: boolean;
}

/** Providers the backend can race — keep in sync with backend/app/main.py's _PROVIDER_MODELS. */
export const PROVIDERS = [
  { id: "groq", label: "Groq" },
  { id: "openrouter", label: "OpenRouter" },
  { id: "cerebras", label: "Cerebras" },
  { id: "mistral", label: "Mistral" },
  { id: "google_ai_studio", label: "Google AI Studio" },
  { id: "cloudflare_workers_ai", label: "Cloudflare Workers AI" },
] as const;

export type ProviderId = (typeof PROVIDERS)[number]["id"];
