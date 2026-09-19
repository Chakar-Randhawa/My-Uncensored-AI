"use client";

import { useActionState } from "react";
import { sendMagicLink, type SignInState } from "./actions";

const initialState: SignInState = { status: "idle" };

export function SignInForm({ next }: { next: string }) {
  const [state, formAction, isPending] = useActionState(sendMagicLink, initialState);

  if (state.status === "sent") {
    return (
      <div className="rounded-md border border-border bg-surface px-4 py-4 text-sm">
        <p className="font-medium">Check your inbox</p>
        <p className="mt-1 text-muted-foreground">
          We sent a sign-in link to <span className="text-foreground">{state.email}</span>.
          Open it on this device to continue.
        </p>
      </div>
    );
  }

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <input type="hidden" name="next" value={next} />

      <label className="flex flex-col gap-1.5">
        <span className="text-sm text-muted-foreground">Email</span>
        <input
          type="email"
          name="email"
          required
          autoComplete="email"
          autoFocus
          placeholder="you@company.com"
          className="rounded-md border border-border bg-surface px-3 py-2.5 text-[15px] outline-none placeholder:text-muted-foreground focus:border-accent"
        />
      </label>

      {state.status === "error" && (
        <p className="text-sm text-destructive">{state.message}</p>
      )}

      <button
        type="submit"
        disabled={isPending}
        className="mt-1 rounded-md bg-accent px-4 py-2.5 text-sm font-medium text-accent-foreground disabled:opacity-40"
      >
        {isPending ? "Sending…" : "Send magic link"}
      </button>
    </form>
  );
}
