"use server";

import { createClient } from "@/lib/supabase/server";
import { headers } from "next/headers";

export type SignInState =
  | { status: "idle" }
  | { status: "sent"; email: string }
  | { status: "error"; message: string };

/**
 * Sends a Supabase magic-link email. The link carries the visitor back to
 * `/auth/callback`, which exchanges the one-time code for a session before
 * redirecting on to `next` (see `app/auth/callback/route.ts`).
 */
export async function sendMagicLink(
  _prevState: SignInState,
  formData: FormData,
): Promise<SignInState> {
  const email = String(formData.get("email") ?? "").trim();
  const next = String(formData.get("next") ?? "/chat");

  if (!email || !email.includes("@")) {
    return { status: "error", message: "Enter a valid email address." };
  }

  const supabase = await createClient();
  const requestHeaders = await headers();
  const origin = requestHeaders.get("origin");

  const { error } = await supabase.auth.signInWithOtp({
    email,
    options: {
      emailRedirectTo: `${origin}/auth/callback?next=${encodeURIComponent(next)}`,
      shouldCreateUser: true,
    },
  });

  if (error) {
    return { status: "error", message: error.message };
  }

  return { status: "sent", email };
}
