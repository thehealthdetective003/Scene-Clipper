import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { AnimatePresence, motion } from "motion/react";

import { api } from "./api/client";
import type { SessionInfo } from "./api/types";
import { Button } from "./components/ui/Button";
import { Logo, LogoMark } from "./components/ui/Logo";
import {
  IconArrowRight,
  IconFilm,
  IconLogout,
  IconMenu,
  IconPlus,
  IconSettings,
  IconX,
} from "./components/ui/Icons";
import { cn } from "./lib/cn";
import { SessionContext, useSession } from "./hooks/useSession";
import { JobPage } from "./pages/JobPage";
import { JobsPage } from "./pages/JobsPage";
import { LandingPage } from "./pages/LandingPage";
import { LoginPage } from "./pages/LoginPage";
import { NewJobPage } from "./pages/NewJobPage";
import { SettingsPage } from "./pages/SettingsPage";

/* ------------------------------------------------------------- session */

function SessionProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setSession(await api.session());
    } catch {
      setSession({ authenticated: false, csrfToken: null });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({
      session,
      loading,
      refresh,
      signIn: async (username: string, password: string) => {
        await api.login(username, password);
        await refresh();
      },
      signOut: async () => {
        await api.logout();
        setSession({ authenticated: false, csrfToken: null });
      },
    }),
    [session, loading, refresh],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

function RequireAuth({ children }: { children: React.ReactElement }) {
  const { session, loading } = useSession();
  if (loading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <div className="flex items-center gap-3 text-sm text-ink-400">
          <span className="size-2 animate-pulse rounded-full bg-azure-500" />
          Loading…
        </div>
      </div>
    );
  }
  if (!session?.authenticated) return <Navigate to="/login" replace />;
  return children;
}

/* ------------------------------------------------------------ app chrome */

const NAV = [
  { to: "/jobs", label: "Jobs", icon: IconFilm },
  { to: "/new", label: "New job", icon: IconPlus },
  { to: "/settings", label: "Settings", icon: IconSettings },
] as const;

const MARKETING_LINKS = [
  { href: "#features", label: "Features" },
  { href: "#how", label: "How it works" },
  { href: "#faq", label: "FAQ" },
] as const;

function AppNav() {
  return (
    <nav className="hidden items-center gap-1 rounded-full border border-line bg-canvas p-1 md:flex">
      {NAV.map(({ to, label, icon: Icon }) => (
        <NavLink key={to} to={to} className="relative no-underline">
          {({ isActive }) => (
            <span
              className={cn(
                "relative flex items-center gap-2 rounded-full px-4 py-1.5 text-sm font-medium transition-colors",
                isActive ? "text-navy-900" : "text-ink-500 hover:text-ink-900",
              )}
            >
              {isActive && (
                <motion.span
                  layoutId="nav-pill"
                  transition={{ type: "spring", stiffness: 420, damping: 34 }}
                  className="absolute inset-0 rounded-full bg-white shadow-soft"
                />
              )}
              <Icon className="relative" style={{ height: 16, width: 16 }} />
              <span className="relative">{label}</span>
            </span>
          )}
        </NavLink>
      ))}
    </nav>
  );
}

function MarketingNav() {
  return (
    <nav className="hidden items-center gap-1 md:flex">
      {MARKETING_LINKS.map((link) => (
        <a
          key={link.href}
          href={link.href}
          className="rounded-full px-4 py-1.5 text-sm font-medium text-ink-500 no-underline transition-colors hover:bg-canvas hover:text-ink-900"
        >
          {link.label}
        </a>
      ))}
    </nav>
  );
}

function Header() {
  const { session, signOut } = useSession();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);

  const signedIn = Boolean(session?.authenticated);
  const onLanding = location.pathname === "/";
  const onLogin = location.pathname === "/login";

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  return (
    <header className="sticky top-0 z-50 border-b border-line glass">
      <div className="mx-auto flex h-[68px] w-full max-w-[1440px] items-center justify-between gap-4 px-4 sm:px-6">
        <Link to={signedIn && !onLanding ? "/jobs" : "/"} className="no-underline">
          <Logo size={38} />
        </Link>

        {onLanding ? <MarketingNav /> : signedIn && <AppNav />}

        <div className="flex items-center gap-2">
          {signedIn ? (
            <>
              {onLanding ? (
                <Link to="/jobs" className="no-underline">
                  <Button variant="primary" size="sm">
                    Open the app
                    <IconArrowRight style={{ height: 15, width: 15 }} />
                  </Button>
                </Link>
              ) : (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void signOut()}
                  className="hidden sm:inline-flex"
                >
                  <IconLogout style={{ height: 16, width: 16 }} />
                  Sign out
                </Button>
              )}
            </>
          ) : (
            !onLogin && (
              <Link to="/login" className="no-underline">
                <Button variant="primary" size="sm">
                  Get started
                  <IconArrowRight style={{ height: 15, width: 15 }} />
                </Button>
              </Link>
            )
          )}

          {(signedIn || onLanding) && (
            <button
              type="button"
              onClick={() => setMenuOpen((open) => !open)}
              aria-label={menuOpen ? "Close menu" : "Open menu"}
              aria-expanded={menuOpen}
              className="grid size-9 place-items-center rounded-full border border-line bg-white text-ink-600 transition-colors hover:bg-canvas md:hidden"
            >
              {menuOpen ? (
                <IconX style={{ height: 17, width: 17 }} />
              ) : (
                <IconMenu style={{ height: 17, width: 17 }} />
              )}
            </button>
          )}
        </div>
      </div>

      {/* Mobile drawer */}
      <AnimatePresence initial={false}>
        {menuOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden border-t border-line bg-white/95 md:hidden"
          >
            <div className="space-y-1 px-4 py-3">
              {onLanding
                ? MARKETING_LINKS.map((link) => (
                    <a
                      key={link.href}
                      href={link.href}
                      onClick={() => setMenuOpen(false)}
                      className="block rounded-xl px-3 py-2.5 text-sm font-medium text-ink-600 no-underline transition-colors hover:bg-canvas"
                    >
                      {link.label}
                    </a>
                  ))
                : NAV.map(({ to, label, icon: Icon }) => (
                    <NavLink
                      key={to}
                      to={to}
                      className={({ isActive }) =>
                        cn(
                          "flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-sm font-medium no-underline transition-colors",
                          isActive
                            ? "bg-navy-50 text-navy-900"
                            : "text-ink-600 hover:bg-canvas",
                        )
                      }
                    >
                      <Icon style={{ height: 16, width: 16 }} />
                      {label}
                    </NavLink>
                  ))}

              {signedIn && !onLanding && (
                <button
                  type="button"
                  onClick={() => void signOut()}
                  className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2.5 text-sm font-medium text-ink-600 transition-colors hover:bg-canvas"
                >
                  <IconLogout style={{ height: 16, width: 16 }} />
                  Sign out
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </header>
  );
}

function Footer() {
  return (
    <footer className="border-t border-line bg-white/60">
      <div className="mx-auto w-full max-w-[1440px] px-4 py-10 sm:px-6">
        <div className="flex flex-col items-start justify-between gap-6 sm:flex-row sm:items-center">
          <div className="flex items-center gap-3">
            <LogoMark size={36} />
            <div>
              <p className="text-sm font-semibold text-ink-900">Scene Clipper</p>
              <p className="text-xs text-ink-400">Private, self-hosted shot extraction</p>
            </div>
          </div>
          <p className="max-w-sm text-xs leading-relaxed text-ink-400 sm:text-right">
            Shot boundaries are detected locally. Your full source video is never sent to any
            model.
          </p>
        </div>
      </div>
    </footer>
  );
}

/* ------------------------------------------------------------------ app */

function Shell({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  // The landing page manages its own full-bleed sections; the app pages sit in
  // a bounded, padded column.
  const fullBleed = location.pathname === "/";

  return (
    <div className="flex min-h-screen flex-col">
      <Header />
      <main className={cn("flex-1", !fullBleed && "mx-auto w-full max-w-[1440px] px-4 py-8 sm:px-6")}>
        {children}
      </main>
      <Footer />
    </div>
  );
}

export function App() {
  return (
    <SessionProvider>
      <Shell>
        <Routes>
          <Route path="/" element={<LandingPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/jobs"
            element={
              <RequireAuth>
                <JobsPage />
              </RequireAuth>
            }
          />
          <Route
            path="/jobs/:jobId"
            element={
              <RequireAuth>
                <JobPage />
              </RequireAuth>
            }
          />
          <Route
            path="/new"
            element={
              <RequireAuth>
                <NewJobPage />
              </RequireAuth>
            }
          />
          <Route
            path="/settings"
            element={
              <RequireAuth>
                <SettingsPage />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    </SessionProvider>
  );
}
