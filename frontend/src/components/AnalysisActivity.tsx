import { useEffect, useMemo, useState } from "react";

import type { Job } from "../api/types";
import type { ConnectionState } from "../hooks/useJobEvents";
import { IconCheck, IconClock, IconFilm, IconSparkle } from "./ui/Icons";
import { Progress } from "./ui/Primitives";

const PHASE_LABELS: Record<string, string> = {
  queued: "Waiting",
  probing: "Reading video",
  detecting: "Finding scenes",
  measuring: "Measuring quality",
  ranking: "Preparing review",
  previews: "Rendering previews",
  ready: "Complete",
};

const MILESTONES = [
  { label: "Read videos", threshold: 10, phases: ["probing"] },
  { label: "Find scenes", threshold: 62, phases: ["detecting"] },
  { label: "Measure clips", threshold: 82, phases: ["measuring"] },
  { label: "Prepare review", threshold: 100, phases: ["ranking", "previews"] },
] as const;

function sourceName(job: Job, index: number): string {
  const source = job.sources[index];
  return source?.sourceName || source?.fileName || `Source ${index + 1}`;
}

function updatedLabel(updatedAt: string, now: number): string {
  const elapsed = Math.max(0, Math.floor((now - new Date(updatedAt).getTime()) / 1000));
  if (!Number.isFinite(elapsed) || elapsed < 5) return "updated just now";
  if (elapsed < 60) return `updated ${elapsed}s ago`;
  return `updated ${Math.floor(elapsed / 60)}m ago`;
}

function durationLabel(durationUs: number): string {
  const seconds = Math.round(durationUs / 1_000_000);
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

export function AnalysisActivity({
  job,
  connection,
}: {
  job: Job;
  connection: ConnectionState;
}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const readyCount = job.sources.filter((source) => source.progress.phase === "ready").length;
  const activeSources = job.sources.filter(
    (source) => !["queued", "ready"].includes(source.progress.phase),
  );
  const currentActivity = useMemo(() => {
    if (activeSources.length === 0) return job.progress.message;
    if (activeSources.length === 1) {
      const active = activeSources[0];
      const index = job.sources.findIndex((source) => source.id === active?.id);
      return `${sourceName(job, index)} · ${active?.progress.message}`;
    }
    return `${activeSources.length} sources are working in parallel.`;
  }, [activeSources, job]);

  return (
    <section
      aria-label="Live analysis activity"
      className="overflow-hidden rounded-panel border border-line bg-paper shadow-card"
    >
      <div className="border-b border-line bg-gradient-to-r from-navy-50 via-paper to-azure-50 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 items-start gap-3">
            <span className="relative grid size-11 flex-none place-items-center rounded-2xl border border-azure-100 bg-paper text-azure-700 shadow-soft">
              <span className="absolute inset-1 animate-pulse rounded-xl bg-azure-100/50" />
              <IconSparkle className="relative" style={{ height: 20, width: 20 }} />
            </span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-base font-semibold text-ink-900">Live analysis activity</h2>
                <span className="inline-flex items-center gap-1.5 rounded-full border border-azure-100 bg-azure-50 px-2.5 py-1 text-[11px] font-semibold text-azure-700">
                  <span className="relative flex size-1.5">
                    <span className="absolute inset-0 animate-pulse-ring rounded-full bg-current" />
                    <span className="relative size-1.5 rounded-full bg-current" />
                  </span>
                  {connection === "open" ? "Live" : "Refreshing"}
                </span>
              </div>
              <p aria-live="polite" className="mt-1 text-sm text-ink-500">
                {currentActivity}
              </p>
            </div>
          </div>
          <div className="text-right">
            <p className="text-sm font-semibold tabular-nums text-ink-900">
              {readyCount} of {job.sources.length} sources ready
            </p>
            <p className="mt-0.5 text-xs text-ink-400">You can safely leave this page.</p>
          </div>
        </div>

        <div className="mt-5 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {MILESTONES.map((milestone, index) => {
            const completed = job.sources.filter(
              (source) => source.progress.percent >= milestone.threshold,
            ).length;
            const active = job.sources.filter((source) =>
              milestone.phases.some((phase) => phase === source.progress.phase),
            ).length;
            const done = completed === job.sources.length;
            return (
              <div
                key={milestone.label}
                className={`rounded-xl border px-3 py-2.5 ${
                  done
                    ? "border-ok-100 bg-ok-50"
                    : active > 0
                      ? "border-azure-100 bg-azure-50"
                      : "border-line bg-paper/70"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`grid size-6 flex-none place-items-center rounded-full text-[11px] font-bold ${
                      done
                        ? "bg-ok-500 text-white"
                        : active > 0
                          ? "bg-azure-500 text-white"
                          : "bg-canvas-2 text-ink-400"
                    }`}
                  >
                    {done ? <IconCheck style={{ height: 13, width: 13 }} /> : index + 1}
                  </span>
                  <span className="text-xs font-semibold text-ink-700">{milestone.label}</span>
                </div>
                <p className="mt-1.5 pl-8 text-[11px] text-ink-400">
                  {active > 0 ? `${active} active` : `${completed}/${job.sources.length} complete`}
                </p>
              </div>
            );
          })}
        </div>
      </div>

      <div className="grid gap-3 p-4 sm:p-5 lg:grid-cols-2">
        {job.sources.map((source, index) => {
          const ready = source.progress.phase === "ready";
          const queued = source.progress.phase === "queued";
          return (
            <article
              key={source.id}
              className={`rounded-2xl border p-4 transition-colors ${
                ready ? "border-ok-100 bg-ok-50/40" : "border-line bg-canvas"
              }`}
              data-testid={`analysis-source-${index + 1}`}
            >
              <div className="flex items-start gap-3">
                <span
                  className={`relative grid size-10 flex-none place-items-center rounded-xl ${
                    ready ? "bg-ok-100 text-ok-600" : "bg-navy-900 text-white"
                  }`}
                >
                  {!ready && !queued && (
                    <span className="absolute -right-0.5 -top-0.5 size-2.5 animate-pulse rounded-full border-2 border-paper bg-azure-400" />
                  )}
                  {ready ? (
                    <IconCheck style={{ height: 18, width: 18 }} />
                  ) : (
                    <IconFilm style={{ height: 18, width: 18 }} />
                  )}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-[11px] font-semibold uppercase tracking-wider text-ink-400">
                        Source {index + 1}
                      </p>
                      <h3 className="truncate text-sm font-semibold text-ink-900">
                        {sourceName(job, index)}
                      </h3>
                    </div>
                    <span
                      className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${
                        ready
                          ? "bg-ok-100 text-ok-600"
                          : queued
                            ? "bg-paper text-ink-400"
                            : "bg-azure-50 text-azure-700"
                      }`}
                    >
                      {PHASE_LABELS[source.progress.phase] ?? source.progress.phase}
                    </span>
                  </div>
                  <p className="mt-2 min-h-5 text-xs leading-relaxed text-ink-500">
                    {source.progress.message}
                  </p>
                </div>
              </div>

              <div className="mt-3">
                <div className="mb-1.5 flex items-center justify-between gap-3 text-[11px]">
                  <span className="flex items-center gap-1.5 text-ink-400">
                    <IconClock style={{ height: 12, width: 12 }} />
                    {updatedLabel(source.updatedAt, now)}
                  </span>
                  <span className="font-semibold tabular-nums text-ink-600">
                    {Math.round(source.progress.percent)}%
                  </span>
                </div>
                <Progress value={source.progress.percent} className="h-1.5" />
              </div>

              <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-400">
                {source.video && (
                  <span>
                    {source.video.width}×{source.video.height} · {durationLabel(source.video.durationUs)}
                  </span>
                )}
                {source.detectedCount !== null && <span>{source.detectedCount} scenes found</span>}
                {source.eligibleCount !== null && <span>{source.eligibleCount} usable</span>}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
