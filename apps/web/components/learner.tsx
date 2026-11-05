"use client";
// The signed-in learner (Module 16). The session is an HttpOnly cookie the server checks on every
// learner-scoped route; this context only mirrors who that is, for rendering.
import type { Learner } from "@sc/schemas";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, errorText } from "@/lib/api";

type LearnerState = {
  /** The signed-in learner, or null. */
  learner: Learner | null;
  /** False until the session check has answered. */
  ready: boolean;
  error: string | null;
  signIn: (mode: "login" | "signup", name: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
};

const LearnerContext = createContext<LearnerState | null>(null);

export function LearnerProvider({ children }: { children: ReactNode }) {
  const [learner, setLearner] = useState<Learner | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .session()
      .then((s) => setLearner(s.learner))
      .catch((e) => setError(errorText(e)))
      .finally(() => setReady(true));
  }, []);

  const signIn = useCallback(async (mode: "login" | "signup", name: string, password: string) => {
    setLearner(await (mode === "login" ? api.login({ name, password }) : api.signup({ name, password })));
    setError(null);
  }, []);

  const signOut = useCallback(async () => {
    await api.logout();
    setLearner(null);
  }, []);

  return <LearnerContext.Provider value={{ learner, ready, error, signIn, signOut }}>{children}</LearnerContext.Provider>;
}

export function useLearner(): LearnerState {
  const ctx = useContext(LearnerContext);
  if (!ctx) throw new Error("useLearner must be used inside <LearnerProvider>");
  return ctx;
}

/** Header control: the signed-in learner with a sign-out button, or a sign-in link. */
export function AccountControl() {
  const { learner, ready, error, signOut } = useLearner();
  const router = useRouter();
  const pathname = usePathname();
  const [busy, setBusy] = useState(false);

  if (error) return <span className="text-meta text-proof" title={error}>API offline</span>;
  if (!ready) return <span className="h-8 w-20 animate-pulse rounded-card bg-surface" aria-hidden="true" />;
  if (!learner) {
    return (
      <Link
        href={pathname === "/login" ? "/login" : `/login?next=${encodeURIComponent(pathname)}`}
        className="inline-flex h-8 items-center rounded-card bg-ink px-3 text-meta text-paper transition-opacity hover:opacity-85 active:opacity-70"
      >
        Sign in
      </Link>
    );
  }
  return (
    <span className="flex items-center gap-2 text-meta">
      <span className="hidden max-w-36 truncate text-ink sm:inline" title={learner.name}>{learner.name}</span>
      <button
        type="button"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await signOut();
            router.push("/");
          } finally {
            setBusy(false);
          }
        }}
        className="h-8 rounded-card border border-rule-strong px-2.5 text-graphite transition-colors hover:bg-surface hover:text-ink active:bg-rule disabled:opacity-50"
      >
        {busy ? "Signing out…" : "Sign out"}
      </button>
    </span>
  );
}

/** Pages that need an account: personal data (tutor, quiz, report, dashboard) and the course itself (sources). */
const PRIVATE = ["/tutor", "/quiz", "/dashboard", "/sources", "/onboarding", "/map", "/flashcards"];

/** Shows a sign-in prompt instead of a private page when nobody is signed in. */
export function LearnerGate({ children }: { children: ReactNode }) {
  const { learner, ready } = useLearner();
  const pathname = usePathname();
  const isPrivate = PRIVATE.some((p) => pathname === p || pathname.startsWith(`${p}/`));
  // Keyed by path: each page fades in on navigation (CSS .page-in; off under prefers-reduced-motion).
  if (!isPrivate || learner || pathname === "/tutor") {
    return <div key={pathname} className="page-in flex flex-1 flex-col">{children}</div>;
  }
  if (!ready) return <main className="mx-auto w-full max-w-3xl p-8 text-graphite">Checking your session…</main>;
  return (
    <main className="mx-auto w-full max-w-md px-4 py-20 text-center">
      <h1 className="font-display text-title text-ink">Sign in to continue</h1>
      <p className="mt-3 text-body text-graphite">Your course, tutor conversations, quizzes and mastery map are only shown to signed-in accounts.</p>
      <Link
        href={`/login?next=${encodeURIComponent(pathname)}`}
        className="mt-8 inline-flex h-11 items-center rounded-card bg-ink px-6 text-body text-paper transition-opacity hover:opacity-85 active:opacity-70"
      >
        Sign in or create an account
      </Link>
    </main>
  );
}
