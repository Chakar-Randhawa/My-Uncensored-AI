import { createBrowserClient } from "@supabase/ssr";

/**
 * Browser-side Supabase client. Safe to call repeatedly — SSR/browser
 * clients are cheap and stateless beyond the auth cookie they read.
 *
 * Uses the project's `publishable` key (current Supabase key format,
 * `sb_publishable_...`) — a drop-in, equally-low-privilege replacement
 * for the older `anon` key. Same RLS behavior either way.
 */
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
  );
}
