import { useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { motion } from "motion/react";

import { CLIP_RANGE_LABEL } from "../lib/clip";
import { ApiError } from "../api/client";
import { ShimmerButton } from "../components/ui/Button";
import { Alert, Field, Input } from "../components/ui/Primitives";
import { Aurora, GridPattern } from "../components/ui/Spotlight";
import { LogoMark } from "../components/ui/Logo";
import {
  IconArrowRight,
  IconCheck,
  IconCpu,
  IconScissors,
  IconShield,
} from "../components/ui/Icons";
import { useSession } from "../hooks/useSession";

const HIGHLIGHTS = [
  {
    icon: IconScissors,
    title: "Never crosses a cut",
    body: "Every clip lives inside one continuous shot.",
  },
  {
    icon: IconCpu,
    title: "Analysed locally",
    body: "Detection and export run on this machine.",
  },
  {
    icon: IconShield,
    title: "Keys encrypted at rest",
    body: "AES-256-GCM, never returned by the API.",
  },
];

export function LoginPage() {
  const { session, loading, signIn } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!loading && session?.authenticated) return <Navigate to="/jobs" replace />;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(username, password);
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "Sign-in failed. Check your connection and try again.",
      );
    } finally {
      // The password is never retained beyond this submission.
      setPassword("");
      setBusy(false);
    }
  };

  return (
    <div className="relative mx-auto flex min-h-[calc(100vh-13rem)] w-full max-w-6xl items-center py-8">
      <Aurora intensity="soft" />
      <GridPattern />

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
        className="relative grid w-full overflow-hidden rounded-hero border border-line bg-paper shadow-float lg:grid-cols-[1.05fr_1fr]"
      >
        {/* ------------------------------------------- Brand panel */}
        <div className="relative hidden flex-col justify-between overflow-hidden bg-navy-950 p-10 lg:flex">
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0"
            style={{
              background:
                "radial-gradient(32rem 22rem at 12% 0%, rgb(0 163 250 / 0.34), transparent 62%), radial-gradient(28rem 20rem at 100% 100%, rgb(28 80 166 / 0.46), transparent 60%)",
            }}
          />

          <div className="relative">
            <LogoMark size={48} hero />
            <h2 className="display-tight mt-8 max-w-sm text-[2.4rem] font-semibold text-white">
              Welcome back
            </h2>
            <p className="mt-4 max-w-xs text-sm leading-relaxed text-white/60">
              Sign in to turn one long video into its best {CLIP_RANGE_LABEL} second shots —
              ranked, reviewable, and frame-accurate.
            </p>
          </div>

          <ul className="relative mt-12 space-y-5">
            {HIGHLIGHTS.map((item, index) => (
              <motion.li
                key={item.title}
                initial={{ opacity: 0, x: -14 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.5, delay: 0.25 + index * 0.1 }}
                className="flex gap-3.5"
              >
                <span className="grid size-9 flex-none place-items-center rounded-xl bg-white/10 text-azure-300 ring-1 ring-white/10">
                  <item.icon style={{ height: 17, width: 17 }} />
                </span>
                <div>
                  <p className="text-sm font-semibold text-white">{item.title}</p>
                  <p className="mt-0.5 text-xs text-white/50">{item.body}</p>
                </div>
              </motion.li>
            ))}
          </ul>

          <p className="relative mt-12 text-xs text-white/35">
            Private deployment · one shared administrator account
          </p>
        </div>

        {/* ------------------------------------------------- Form */}
        <div className="p-7 sm:p-10 lg:p-12">
          <div className="mb-8 lg:hidden">
            <LogoMark size={44} />
          </div>

          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">Sign in</h1>
          <p className="mt-2 text-sm text-ink-500">
            Use the administrator credentials for this installation.
          </p>

          <form onSubmit={submit} className="mt-8 space-y-5">
            <Field label="Username">
              <Input
                type="text"
                value={username}
                autoComplete="username"
                placeholder="admin"
                onChange={(event) => setUsername(event.target.value)}
                required
              />
            </Field>

            <Field label="Password">
              <Input
                type="password"
                value={password}
                autoComplete="current-password"
                placeholder="••••••••••••"
                onChange={(event) => setPassword(event.target.value)}
                required
              />
            </Field>

            {error && (
              <Alert tone="rose" role="alert">
                {error}
              </Alert>
            )}

            <ShimmerButton type="submit" disabled={busy} className="w-full">
              {busy ? "Signing in…" : "Sign in"}
              {!busy && <IconArrowRight style={{ height: 16, width: 16 }} />}
            </ShimmerButton>
          </form>

          <div className="mt-8 space-y-2.5 border-t border-line pt-6">
            {[
              "No credentials are ever written to browser storage",
              "Sessions are server-side and expire on idle",
            ].map((line) => (
              <p key={line} className="flex items-start gap-2 text-xs text-ink-400">
                <IconCheck
                  style={{ height: 13, width: 13 }}
                  strokeWidth={3}
                  className="mt-px flex-none text-ok-500"
                />
                {line}
              </p>
            ))}
          </div>

          <p className="mt-6 text-xs text-ink-400">
            New here?{" "}
            <Link to="/" className="font-medium text-azure-700 hover:text-azure-800">
              See what Scene Clipper does
            </Link>
          </p>
        </div>
      </motion.div>
    </div>
  );
}
