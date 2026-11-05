"use client";
// Site header: brand, main navigation (the current page highlighted), the notebook switcher and the
// account menu. Below lg it collapses into a menu button with a full-width panel.
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useLearner } from "@/components/learner";
import { NotebookSwitcher } from "@/components/notebook-switcher";

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/tutor", label: "Tutor" },
  { href: "/map", label: "Map" },
  { href: "/flashcards", label: "Flashcards" },
  { href: "/quiz", label: "Quiz" },
  { href: "/sources", label: "Sources" },
] as const;

const isActive = (pathname: string, href: string) => pathname === href || pathname.startsWith(`${href}/`);
const subscribeToHydration = () => () => {};
const getHydratedSnapshot = () => true;
const getServerHydrationSnapshot = () => false;

function AccountMenu({ hydrated, onNavigate }: { hydrated: boolean; onNavigate?: () => void }) {
  const { learner, ready, signOut } = useLearner();
  const router = useRouter();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  if (!hydrated || !ready) return <span className="h-9 w-24 animate-pulse rounded-full bg-surface" aria-hidden="true" />;
  if (!learner) {
    return (
      <Link
        href={pathname === "/login" ? "/login" : `/login?next=${encodeURIComponent(pathname)}`}
        onClick={onNavigate}
        className="inline-flex h-9 items-center rounded-full bg-ink px-4 text-meta font-medium text-paper hover:opacity-85"
      >
        Sign in
      </Link>
    );
  }
  const go = (href: string) => {
    setOpen(false);
    onNavigate?.();
    router.push(href);
  };
  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex h-9 items-center gap-2 rounded-full border border-rule-strong bg-surface pl-1 pr-3 text-meta text-ink hover:border-ink"
      >
        <span className="flex h-7 w-7 items-center justify-center rounded-full bg-proof text-xs font-bold uppercase text-white" aria-hidden="true">
          {learner.name.trim().charAt(0) || "?"}
        </span>
        <span className="max-w-28 truncate">{learner.name}</span>
      </button>
      {open && (
        <div role="menu" className="absolute right-0 top-11 z-50 w-56 overflow-hidden rounded-2xl border border-rule bg-surface py-2 shadow-lg">
          <p className="px-4 pb-2 pt-1 text-xs text-graphite">Signed in as <span className="font-medium text-ink">{learner.name}</span></p>
          <button role="menuitem" type="button" onClick={() => go("/onboarding")} className="block w-full px-4 py-2 text-left text-sm text-ink hover:bg-paper">Tutor personality</button>
          <button role="menuitem" type="button" onClick={() => go("/evaluation")} className="block w-full px-4 py-2 text-left text-sm text-ink hover:bg-paper">Evaluation</button>
          <div className="my-1 border-t border-rule" />
          <button
            role="menuitem"
            type="button"
            onClick={async () => {
              setOpen(false);
              onNavigate?.();
              await signOut();
              router.push("/");
            }}
            className="block w-full px-4 py-2 text-left text-sm text-proof hover:bg-paper"
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export function SiteHeader() {
  const pathname = usePathname();
  const { learner } = useLearner();
  const [menu, setMenu] = useState(false);
  const hydrated = useSyncExternalStore(subscribeToHydration, getHydratedSnapshot, getServerHydrationSnapshot);
  const close = () => setMenu(false);

  return (
    <header className="sticky top-0 z-30 border-b border-rule bg-paper/90 backdrop-blur">
      <div className="mx-auto flex h-16 w-full max-w-7xl items-center gap-4 px-4 sm:px-6">
        <Link href="/" onClick={close} className="flex shrink-0 items-center gap-2.5">
          <span aria-hidden="true" className="locator flex h-7 w-7 items-center justify-center rounded-card bg-ink text-paper">¶</span>
          <span className="font-display text-lg font-semibold tracking-[-0.02em] text-ink">Omnitutor</span>
        </Link>

        <nav aria-label="Main" className="hidden flex-1 lg:block">
          <ul className="flex items-center gap-1">
            {NAV.map((l) => {
              const active = isActive(pathname, l.href);
              return (
                <li key={l.href}>
                  <Link
                    href={l.href}
                    aria-current={active ? "page" : undefined}
                    className={`inline-flex h-9 items-center rounded-full px-3.5 text-meta transition-colors ${active ? "bg-ink text-paper" : "text-graphite hover:bg-surface hover:text-ink"}`}
                  >
                    {l.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="ml-auto hidden items-center gap-3 lg:flex">
          {hydrated && learner && <NotebookSwitcher />}
          <AccountMenu hydrated={hydrated} />
        </div>

        <button
          type="button"
          onClick={() => setMenu((m) => !m)}
          aria-expanded={menu}
          aria-controls="mobile-menu"
          aria-label={menu ? "Close menu" : "Open menu"}
          className="ml-auto flex h-10 w-10 items-center justify-center rounded-full border border-rule-strong bg-surface lg:hidden"
        >
          <span className="relative block h-3.5 w-4" aria-hidden="true">
            <span className={`absolute left-0 h-0.5 w-4 rounded bg-ink transition-transform ${menu ? "top-1.5 rotate-45" : "top-0"}`} />
            <span className={`absolute left-0 top-1.5 h-0.5 w-4 rounded bg-ink transition-opacity ${menu ? "opacity-0" : ""}`} />
            <span className={`absolute left-0 h-0.5 w-4 rounded bg-ink transition-transform ${menu ? "top-1.5 -rotate-45" : "top-3"}`} />
          </span>
        </button>
      </div>

      {menu && (
        <div id="mobile-menu" className="border-t border-rule bg-paper px-4 pb-6 pt-3 lg:hidden">
          <ul className="grid grid-cols-2 gap-2">
            {NAV.map((l) => {
              const active = isActive(pathname, l.href);
              return (
                <li key={l.href}>
                  <Link href={l.href} onClick={close} aria-current={active ? "page" : undefined} className={`flex h-11 items-center rounded-2xl px-4 text-sm ${active ? "bg-ink text-paper" : "border border-rule bg-surface text-ink"}`}>
                    {l.label}
                  </Link>
                </li>
              );
            })}
          </ul>
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
            {hydrated && learner && <NotebookSwitcher />}
            <AccountMenu hydrated={hydrated} onNavigate={close} />
          </div>
        </div>
      )}
    </header>
  );
}
