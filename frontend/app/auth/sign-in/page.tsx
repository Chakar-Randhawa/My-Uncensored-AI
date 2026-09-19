import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { SignInForm } from "./SignInForm";

interface SignInPageProps {
  searchParams: Promise<{ next?: string; error?: string }>;
}

export default async function SignInPage({ searchParams }: SignInPageProps) {
  const { next = "/chat", error } = await searchParams;

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (user) {
    redirect(next);
  }

  return (
    <div className="flex h-screen items-center justify-center bg-background text-foreground">
      <div className="w-full max-w-sm px-6">
        <div className="mb-6 flex items-center gap-2">
          <div className="h-2 w-2 rounded-full bg-accent" />
          <span className="text-sm font-medium">AI Router</span>
        </div>

        <h1 className="mb-1 text-lg font-medium">Sign in</h1>
        <p className="mb-6 text-sm text-muted-foreground">
          No password needed — we&apos;ll email you a one-time link.
        </p>

        {error && (
          <p className="mb-4 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </p>
        )}

        <SignInForm next={next} />
      </div>
    </div>
  );
}
