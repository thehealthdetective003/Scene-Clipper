import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";

import { MAX_CLIP_SECONDS } from "../lib/clip";
import { ApiError, api } from "../api/client";
import type { GeminiKey, GeminiKeyStatus, GeminiSettings } from "../api/types";
import { cn } from "../lib/cn";
import { Button } from "../components/ui/Button";
import {
  IconAlert,
  IconCheck,
  IconKey,
  IconPlus,
  IconRefresh,
  IconSparkle,
  IconTrash,
  IconX,
} from "../components/ui/Icons";
import {
  Alert,
  Badge,
  Field,
  Input,
  Panel,
  PanelHeader,
  Rise,
} from "../components/ui/Primitives";
import { SpotlightCard } from "../components/ui/Spotlight";
import { SourceLabelSettingsPanel } from "../components/SourceLabelSettingsPanel";

/** How each health state reads, and how loudly it reads. */
const STATUS_META: Record<
  GeminiKeyStatus,
  { label: string; tone: "emerald" | "rose" | "amber" | "neutral"; hint: string }
> = {
  active: {
    label: "Active",
    tone: "emerald",
    hint: "Ready. Used in order, top to bottom.",
  },
  exhausted: {
    label: "Limit hit",
    tone: "rose",
    hint: "This key is out of quota. Jobs have moved on to the next key.",
  },
  invalid: {
    label: "Rejected",
    tone: "rose",
    hint: "The provider refused this key. Delete it and add a working one.",
  },
  unavailable: {
    label: "Unavailable",
    tone: "amber",
    hint: "The provider could not be reached with this key. It will be retried shortly.",
  },
  disabled: {
    label: "Off",
    tone: "neutral",
    hint: "Switched off. Skipped during failover until you turn it back on.",
  },
};

/** The two states the user asked to see in red the moment they happen. */
const isFailed = (status: GeminiKeyStatus) => status === "exhausted" || status === "invalid";

function relativeTime(iso: string | null): string | null {
  if (!iso) return null;
  const delta = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(delta)) return null;
  const minutes = Math.round(delta / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function cooldownLabel(iso: string | null): string | null {
  if (!iso) return null;
  const seconds = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  if (seconds < 60) return `retries in ${seconds}s`;
  return `retries in ${Math.ceil(seconds / 60)}m`;
}

/**
 * One row in the failover pool. Shows what the key is doing right now; never
 * the key itself, which the server does not return in any form.
 */
function KeyRow({
  entry,
  index,
  busy,
  onTest,
  onToggle,
  onDelete,
}: {
  entry: GeminiKey;
  index: number;
  busy: boolean;
  onTest: () => void;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const meta = STATUS_META[entry.status];
  const failed = isFailed(entry.status);
  const cooldown = cooldownLabel(entry.cooldownUntil);
  const lastError = relativeTime(entry.lastErrorAt);
  const lastSuccess = relativeTime(entry.lastSuccessAt);

  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -8 }}
      transition={{ duration: 0.18 }}
      className={cn(
        "rounded-xl border px-4 py-3.5 transition-colors",
        failed
          ? "border-bad-500/45 bg-bad-50"
          : entry.status === "disabled"
            ? "border-line bg-canvas opacity-60"
            : "border-line bg-paper",
      )}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span
          className={cn(
            "grid size-7 flex-none place-items-center rounded-lg text-xs font-semibold tabular-nums",
            failed ? "bg-bad-100 text-bad-600" : "bg-canvas-2 text-ink-500",
          )}
          title={`Failover order: tried ${index === 0 ? "first" : `#${index + 1}`}`}
        >
          {index + 1}
        </span>

        <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink-700">
          {entry.label}
        </span>

        <Badge tone={meta.tone} dot>
          {failed && <IconAlert style={{ height: 12, width: 12 }} />}
          {meta.label}
        </Badge>

        <div className="flex flex-none gap-1">
          <Button
            variant="ghost"
            onClick={onTest}
            disabled={busy}
            className="px-2 py-1.5 text-xs"
            title="Re-check this key against the provider (uses one request)"
          >
            <IconRefresh style={{ height: 14, width: 14 }} />
          </Button>
          <Button
            variant="ghost"
            onClick={onToggle}
            disabled={busy}
            className="px-2 py-1.5 text-xs"
            title={entry.status === "disabled" ? "Put back in rotation" : "Take out of rotation"}
          >
            {entry.status === "disabled" ? (
              <IconCheck style={{ height: 14, width: 14 }} />
            ) : (
              <IconX style={{ height: 14, width: 14 }} />
            )}
          </Button>
          <Button
            variant="ghost"
            onClick={onDelete}
            disabled={busy}
            className="px-2 py-1.5 text-xs text-bad-600 hover:bg-bad-50"
            title="Delete this key"
          >
            <IconTrash style={{ height: 14, width: 14 }} />
          </Button>
        </div>
      </div>

      <p className={cn("mt-2 text-xs", failed ? "text-bad-600" : "text-ink-400")}>
        {meta.hint}
        {cooldown && ` ${cooldown}.`}
      </p>

      <p className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-ink-300 tabular-nums">
        <span>{entry.requestsSucceeded} ok</span>
        <span>{entry.requestsFailed} failed</span>
        {lastSuccess && <span>last worked {lastSuccess}</span>}
        {failed && lastError && <span>failed {lastError}</span>}
      </p>
    </motion.li>
  );
}

/**
 * A key lives in component state only while this form is open and is cleared
 * the moment it is submitted. It is never written to localStorage,
 * sessionStorage, IndexedDB, or a URL (spec 5.2).
 */
export function SettingsPage() {
  const [settings, setSettings] = useState<GeminiSettings | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [label, setLabel] = useState("");
  const [model, setModel] = useState("");
  const [requestCap, setRequestCap] = useState(8);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const apply = (next: GeminiSettings) => {
    setSettings(next);
    setModel(next.model ?? "");
    setRequestCap(next.requestCap);
  };

  const load = async () => {
    try {
      apply(await api.geminiSettings());
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load settings.");
    }
  };

  useEffect(() => {
    void load();
  }, []);

  // A cooldown expires on its own, so refresh while any key is resting.
  useEffect(() => {
    const resting = settings?.keys.some((key) => key.cooldownUntil && !key.available);
    if (!resting) return;
    const timer = window.setInterval(() => void load(), 15_000);
    return () => window.clearInterval(timer);
  }, [settings]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      await action();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The request failed.");
    } finally {
      setBusy(false);
    }
  };

  const keys = settings?.keys ?? [];
  const maxKeys = settings?.maxKeys ?? 5;
  const poolIsFull = keys.length >= maxKeys;
  const failedCount = keys.filter((key) => isFailed(key.status)).length;
  const capIsValid = Number.isInteger(requestCap) && requestCap >= 0 && requestCap <= 50;

  const test = () =>
    run(async () => {
      const result = await api.testGeminiKey(apiKey, model || undefined);
      if (result.valid) setStatus(result.message);
      else setError(result.message);
    });

  const add = () =>
    run(async () => {
      apply(await api.addGeminiKey(apiKey, model || undefined, requestCap, label || undefined));
      setApiKey(""); // Clear the plaintext key as soon as it is stored.
      setLabel("");
      setStatus("Key added. It is stored encrypted and is never shown again.");
    });

  const savePreferences = () =>
    run(async () => {
      apply(await api.saveGeminiPreferences(model || undefined, requestCap));
      setStatus("Model and request cap saved.");
    });

  const testStored = (entry: GeminiKey) =>
    run(async () => {
      const result = await api.testStoredGeminiKey(entry.id);
      await load();
      if (result.valid) setStatus(`${entry.label} is working.`);
      else setError(`${entry.label}: ${result.message}`);
    });

  const toggle = (entry: GeminiKey) =>
    run(async () => {
      apply(await api.setGeminiKeyEnabled(entry.id, entry.status === "disabled"));
    });

  const remove = (entry: GeminiKey) =>
    run(async () => {
      apply(await api.deleteGeminiKey(entry.id));
      setStatus(`${entry.label} deleted.`);
    });

  const removeAll = () =>
    run(async () => {
      await api.deleteAllGeminiKeys();
      await load();
      setStatus("All keys deleted. New jobs will rank clips locally.");
    });

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900 sm:text-3xl">
          Settings
        </h1>
        <p className="mt-1.5 text-sm text-ink-500">
          Optional Gemini ranking. Detection and export always run locally.
        </p>
      </div>

      <SourceLabelSettingsPanel />

      <Rise>
        <SpotlightCard className="p-5 sm:p-6">
          <PanelHeader
            title={
              <span className="flex items-center gap-2">
                <IconKey style={{ height: 16, width: 16 }} />
                API keys
              </span>
            }
            description={`Up to ${maxKeys} keys. Jobs use them in order and move to the next one the moment a key is rejected or runs out of quota.`}
            actions={
              keys.length === 0 ? (
                <Badge tone="neutral">Local only</Badge>
              ) : settings?.anyAvailable ? (
                <Badge tone="emerald" dot>
                  {keys.length}/{maxKeys} ready
                </Badge>
              ) : (
                <Badge tone="rose" dot>
                  No key available
                </Badge>
              )
            }
          />

          {failedCount > 0 && (
            <Alert tone="rose" className="mb-4">
              <span className="flex gap-2">
                <IconAlert className="mt-0.5 flex-none" style={{ height: 14, width: 14 }} />
                {failedCount === 1
                  ? "One key is not working right now — it is marked in red below."
                  : `${failedCount} keys are not working right now — they are marked in red below.`}
              </span>
            </Alert>
          )}

          {keys.length > 0 ? (
            <motion.ul layout className="mb-5 space-y-2">
              <AnimatePresence initial={false}>
                {keys.map((entry, index) => (
                  <KeyRow
                    key={entry.id}
                    entry={entry}
                    index={index}
                    busy={busy}
                    onTest={() => void testStored(entry)}
                    onToggle={() => void toggle(entry)}
                    onDelete={() => void remove(entry)}
                  />
                ))}
              </AnimatePresence>
            </motion.ul>
          ) : (
            <p className="mb-5 rounded-xl border border-dashed border-line-strong px-4 py-6 text-center text-sm text-ink-400">
              No keys configured. Jobs rank clips using local measurements only.
            </p>
          )}

          <Alert tone="azure" className="mb-5">
            <span className="flex gap-2">
              <IconSparkle className="mt-0.5 flex-none" style={{ height: 14, width: 14 }} />
              Ranking sends representative still frames, and rarely a short low-resolution
              excerpt. Your full source video is never sent to Gemini.
            </span>
          </Alert>

          <div className="space-y-4">
            <Field
              label={poolIsFull ? `Add a key (${maxKeys} of ${maxKeys} stored)` : "Add a key"}
              hint={
                poolIsFull
                  ? "The pool is full. Delete a key before adding another."
                  : "The key is checked against the provider before it is stored."
              }
            >
              <Input
                type="password"
                value={apiKey}
                autoComplete="off"
                spellCheck={false}
                disabled={poolIsFull}
                placeholder="Paste your Gemini API key"
                onChange={(event) => setApiKey(event.target.value)}
              />
            </Field>

            <Field label="Label" hint="Optional. Helps you tell your keys apart.">
              <Input
                type="text"
                value={label}
                spellCheck={false}
                maxLength={64}
                disabled={poolIsFull}
                placeholder={`Key ${keys.length + 1}`}
                onChange={(event) => setLabel(event.target.value)}
              />
            </Field>

            <Field label="Model">
              <Input
                type="text"
                value={model}
                spellCheck={false}
                placeholder="gemini-3.8-flash"
                onChange={(event) => setModel(event.target.value)}
              />
            </Field>

            <Field
              label="Request cap per job"
              hint="0 forces local-only ranking. Every model request counts — retries and calls that moved to another key included."
              error={!capIsValid ? "The cap must be a whole number from 0 to 50." : undefined}
            >
              <div className="flex items-center gap-4">
                <input
                  type="range"
                  min={0}
                  max={50}
                  value={Number.isFinite(requestCap) ? requestCap : 0}
                  onChange={(event) => setRequestCap(Number(event.target.value))}
                  className="flex-1"
                />
                <Input
                  type="number"
                  min={0}
                  max={50}
                  value={requestCap}
                  onChange={(event) => setRequestCap(Number.parseInt(event.target.value, 10))}
                  className="w-20 text-center tabular-nums"
                />
              </div>
            </Field>
          </div>

          {status && (
            <Alert tone="emerald" className="mt-4">
              <span className="flex gap-2">
                <IconCheck className="mt-0.5 flex-none" style={{ height: 14, width: 14 }} />
                {status}
              </span>
            </Alert>
          )}
          {error && (
            <Alert tone="rose" className="mt-4" role="alert">
              {error}
            </Alert>
          )}

          <div className="mt-6 flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() => void test()}
              disabled={busy || !apiKey}
            >
              Test key
            </Button>
            <Button
              variant="primary"
              onClick={() => void add()}
              loading={busy}
              disabled={busy || !apiKey || !capIsValid || poolIsFull}
            >
              <IconPlus style={{ height: 15, width: 15 }} />
              Add key
            </Button>
            <Button
              variant="outline"
              onClick={() => void savePreferences()}
              disabled={busy || !capIsValid}
            >
              Save model &amp; cap
            </Button>
            <Button
              variant="danger"
              onClick={() => void removeAll()}
              disabled={busy || keys.length === 0}
              className="ml-auto"
            >
              <IconTrash style={{ height: 15, width: 15 }} />
              Delete all
            </Button>
          </div>
        </SpotlightCard>
      </Rise>

      <Rise delay={0.08}>
        <Panel>
          <PanelHeader
            title="How ranking works"
            description="Gemini never decides where a shot begins or ends."
          />
          <ul className="space-y-2.5 text-sm text-ink-500">
            {[
              "Shot boundaries and export timecodes are determined locally by FFmpeg and PySceneDetect.",
              `Gemini only ranks shots that already passed the continuity gate, and picks the best ${MAX_CLIP_SECONDS}-second region inside a long shot.`,
              "When a key is rejected or hits its limit, the same request is re-sent on the next key in the list and the failed one turns red here.",
              "If every key is unavailable or the cap runs out, ranking falls back to local measurements and no analysis is lost.",
            ].map((line) => (
              <li key={line} className="flex gap-2.5">
                <span className="mt-1.5 size-1.5 flex-none rounded-full bg-azure-500" />
                {line}
              </li>
            ))}
          </ul>
        </Panel>
      </Rise>
    </div>
  );
}
