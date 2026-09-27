import { useCallback, useEffect, useRef, useState } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { AnimatePresence, motion } from "motion/react";

import { api } from "./api/client";
import { Button } from "./components/ui/Button";
import { Logo, LogoMark } from "./components/ui/Logo";
import { ThemeSwitcher } from "./components/ui/ThemeSwitcher";
import {
  IconArrowRight,
  IconFilm,
  IconMenu,
  IconPlus,
  IconSettings,
  IconX,
} from "./components/ui/Icons";
import { cn } from "./lib/cn";
import { JobPage } from "./pages/JobPage";
import { JobsPage } from "./pages/JobsPage";
import { LandingPage } from "./pages/LandingPage";
import { NewJobPage } from "./pages/NewJobPage";
import { SettingsPage } from "./pages/SettingsPage";

/* ------------------------------------------------------------- session */

function SessionBootstrap({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const startupRequested = useRef(false);

  const start = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const session = await api.session();
      if (!session.authenticated || !session.csrfToken) throw new Error("Session unavailable");
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (startupRequested.current) return;
    startupRequested.current = true;
    void start();
  }, [start]);

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas">
        <div className="flex items-center gap-3 text-sm text-ink-400">
          <span className="size-2 animate-pulse rounded-full bg-azure-500" />
          Opening Scene Clipper…
        </div>
      </div>
    );
  }
  if (failed) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas px-4">
        <div className="w-full max-w-md rounded-3xl border border-line bg-paper p-8 text-center shadow-float">
          <div className="flex justify-center">
            <LogoMark size={44} />
          </div>
          <h1 className="mt-5 text-xl font-semibold text-ink-900">Could not open the app</h1>
          <p className="mt-2 text-sm leading-relaxed text-ink-500">
            Scene Clipper could not start its local session. Check that the backend is running,
            then try again.
          </p>
          <Button variant="primary" className="mt-6" onClick={() => void start()}>
            Try again
          </Button>
        </div>
      </div>
    );
  }
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
                isActive ? "text-ink-900" : "text-ink-500 hover:text-ink-900",
              )}
            >
              {isActive && (
                <motion.span
                  layoutId="nav-pill"
                  transition={{ type: "spring", stiffness: 420, damping: 34 }}
                  className="absolute inset-0 rounded-full bg-paper shadow-soft"
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
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);

  const onLanding = location.pathname === "/";

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  return (
    <header className="sticky top-0 z-50 border-b border-line glass">
      <div className="mx-auto flex h-[68px] w-full max-w-[1440px] items-center justify-between gap-4 px-4 sm:px-6">
        <Link to={onLanding ? "/" : "/jobs"} className="no-underline">
          <Logo size={38} />
        </Link>

        {onLanding ? <MarketingNav /> : <AppNav />}

        <div className="flex items-center gap-2">
          <ThemeSwitcher />

          {onLanding && (
            <Link to="/jobs" className="no-underline">
              <Button variant="primary" size="sm">
                Open the app
                <IconArrowRight style={{ height: 15, width: 15 }} />
              </Button>
            </Link>
          )}

          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-label={menuOpen ? "Close menu" : "Open menu"}
            aria-expanded={menuOpen}
            className="grid size-9 place-items-center rounded-full border border-line bg-paper text-ink-600 transition-colors hover:bg-canvas md:hidden"
          >
            {menuOpen ? (
              <IconX style={{ height: 17, width: 17 }} />
            ) : (
              <IconMenu style={{ height: 17, width: 17 }} />
            )}
          </button>
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
            className="overflow-hidden border-t border-line bg-paper/95 md:hidden"
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
                            ? "bg-navy-50 text-ink-900"
                            : "text-ink-600 hover:bg-canvas",
                        )
                      }
                    >
                      <Icon style={{ height: 16, width: 16 }} />
                      {label}
                    </NavLink>
                  ))}

            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </header>
  );
}

function Footer() {
  return (
    <footer className="border-t border-line bg-paper/60">
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
    <SessionBootstrap>
      <Shell>
        <Routes>
          <Route path="/" element={<LandingPage />} />
          <Route path="/login" element={<Navigate to="/jobs" replace />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:jobId" element={<JobPage />} />
          <Route path="/new" element={<NewJobPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    </SessionBootstrap>
  );
}
