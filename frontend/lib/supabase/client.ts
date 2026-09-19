import { createBrowserClient } from "@supabase/ssr";

/**
 * Browser-side Supabase client. Safe to call repeatedly — SSR/browser
 * clients are cheap and stateless beyond the auth cookie they read.
 */
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
  );
}
