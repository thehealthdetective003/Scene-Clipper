import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { CLIP_RANGE_LABEL } from "../lib/clip";
import { ApiError, api, newIdempotencyKey } from "../api/client";
import type {
  CandidateShot,
  ExportRecord,
  Job,
  ReviewClipInput,
  SelectedClip,
} from "../api/types";
import { CandidateCard } from "../components/CandidateCard";
import { ExportPanel } from "../components/ExportPanel";
import { Button } from "../components/ui/Button";
import {
  IconAlert,
  IconFilm,
  IconLayers,
  IconRefresh,
  IconSparkle,
  IconX,
} from "../components/ui/Icons";
import {
  Alert,
  EmptyState,
  Panel,
  Progress,
  Rise,
  SegmentedControl,
  StatusBadge,
} from "../components/ui/Primitives";
import { useJobEvents } from "../hooks/useJobEvents";
import { stateLabel, warningLabel } from "../lib/format";

type Order = "rank" | "source";

const ORDERS = [
  { value: "rank", label: "Score" },
  { value: "source", label: "Source time" },
] as const;

const RUNNING = ["uploaded", "probing", "detecting", "ranking"];

export function JobPage() {
  const { jobId = "" } = useParams();
  const [job, setJob] = useState<Job | null>(null);
  const [candidates, setCandidates] = useState<CandidateShot[]>([]);
  const [clips, setClips] = useState<SelectedClip[]>([]);
  const [revision, setRevision] = useState(0);
  const [order, setOrder] = useState<Order>("rank");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [activeExport, setActiveExport] = useState<ExportRecord | null>(null);
  const dragged = useRef<string | null>(null);

  const loadJob = useCallback(async () => {
    try {
      setJob(await api.getJob(jobId));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load the job.");
    }
  }, [jobId]);

  const loadCandidates = useCallback(async () => {
    try {
      const body = await api.candidates(jobId);
      setCandidates(body.candidates);
      setClips(body.selectedClips);
      setRevision(body.reviewRevision);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load candidates.");
    }
  }, [jobId]);

  useEffect(() => {
    void loadJob();
  }, [loadJob]);

  // Live progress; a disconnect never affects the worker (spec 5.5).
  const { connection } = useJobEvents(jobId, {
    onEvent: (event) => {
      if (event.type === "heartbeat") return;
      void loadJob();
      if (event.type === "candidates.ready") void loadCandidates();
      if (event.type === "export.ready") {
        const exportId = event.payload?.exportId;
        if (typeof exportId === "string") {
          void api.getExport(jobId, exportId).then(setActiveExport).catch(() => undefined);
        }
      }
    },
  });

  const reviewReady =
    job?.state === "review-ready" || job?.state === "complete" || job?.state === "exporting";

  useEffect(() => {
    if (reviewReady && candidates.length === 0) void loadCandidates();
  }, [reviewReady, candidates.length, loadCandidates]);

  useEffect(() => {
    const latest = job?.latestExport;
    if (latest && latest.id !== activeExport?.id) {
      void api.getExport(jobId, latest.id).then(setActiveExport).catch(() => undefined);
    }
  }, [job?.latestExport, activeExport?.id, jobId]);

  const clipByCandidate = useMemo(() => {
    const map = new Map<string, SelectedClip>();
    for (const clip of clips) map.set(clip.candidateId, clip);
    return map;
  }, [clips]);

  const ordered = useMemo(() => {
    const copy = [...candidates];
    const sourceOrder = new Map(job?.sources.map((source) => [source.id, source.order]) ?? []);
    copy.sort((a, b) => {
      if (order === "rank") return a.rank - b.rank;
      const sourceDifference =
        (sourceOrder.get(a.sourceId) ?? 0) - (sourceOrder.get(b.sourceId) ?? 0);
      return sourceDifference || a.sourceStartUs - b.sourceStartUs;
    });
    return copy;
  }, [candidates, job?.sources, order]);

  const candidateGroups = useMemo(() => {
    const grouped = (job?.sources ?? []).map((source) => ({
      id: source.id,
      name: source.sourceName || source.fileName,
      fileName: source.fileName,
      candidates: ordered.filter((candidate) => candidate.sourceId === source.id),
    }));
    const known = new Set(grouped.map((group) => group.id));
    const unmatched = ordered.filter((candidate) => !known.has(candidate.sourceId));
    if (unmatched.length > 0) {
      grouped.push({
        id: "unmatched",
        name: "Other source",
        fileName: unmatched[0]?.sourceFileName ?? "Source video",
        candidates: unmatched,
      });
    }
    return grouped.filter((group) => group.candidates.length > 0);
  }, [job?.sources, ordered]);

  /** Push the current selection to the server; a stale revision reloads state. */
  const submit = async (next: SelectedClip[]) => {
    setBusy(true);
    setError(null);
    const payload: ReviewClipInput[] = next
      .slice()
      .sort((a, b) => a.order - b.order)
      .map((clip, index) => ({
        candidateId: clip.candidateId,
        order: index + 1,
        startUs: clip.startUs,
        endUs: clip.endUs,
      }));

    try {
      const response = await api.replaceReview(jobId, revision, payload);
      setRevision(response.reviewRevision);
      setClips(response.clips);
      await loadJob();
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "stale_review_revision") {
        setError("The review changed elsewhere. The latest version has been loaded.");
      } else {
        setError(caught instanceof ApiError ? caught.message : "The review could not be saved.");
      }
      await loadCandidates();
    } finally {
      setBusy(false);
    }
  };

  const toggle = (candidate: CandidateShot, selected: boolean) => {
    const next = selected
      ? [
          ...clips,
          {
            id: `pending-${candidate.id}`,
            candidateId: candidate.id,
            order: clips.length + 1,
            startUs: candidate.recommendedStartUs,
            endUs: candidate.recommendedEndUs,
            durationUs: candidate.recommendedEndUs - candidate.recommendedStartUs,
          } satisfies SelectedClip,
        ]
      : clips.filter((clip) => clip.candidateId !== candidate.id);
    void submit(next);
  };

  const trim = (candidateId: string, startUs: number, endUs: number) =>
    setClips((previous) =>
      previous.map((clip) =>
        clip.candidateId === candidateId
          ? { ...clip, startUs, endUs, durationUs: endUs - startUs }
          : clip,
      ),
    );

  const restore = (candidate: CandidateShot) =>
    void submit(
      clips.map((clip) =>
        clip.candidateId === candidate.id
          ? {
              ...clip,
              startUs: candidate.recommendedStartUs,
              endUs: candidate.recommendedEndUs,
              durationUs: candidate.recommendedEndUs - candidate.recommendedStartUs,
            }
          : clip,
      ),
    );

  const reorder = (from: number, to: number) => {
    const sorted = clips.slice().sort((a, b) => a.order - b.order);
    if (from < 0 || to < 0 || from >= sorted.length || to >= sorted.length) return;
    const [moved] = sorted.splice(from, 1);
    if (!moved) return;
    sorted.splice(to, 0, moved);
    void submit(sorted.map((clip, position) => ({ ...clip, order: position + 1 })));
  };

  const move = (candidateId: string, direction: -1 | 1) => {
    const sorted = clips.slice().sort((a, b) => a.order - b.order);
    const index = sorted.findIndex((clip) => clip.candidateId === candidateId);
    reorder(index, index + direction);
  };

  const dropOn = (candidateId: string) => {
    const source = dragged.current;
    dragged.current = null;
    if (!source || source === candidateId) return;
    const sorted = clips.slice().sort((a, b) => a.order - b.order);
    reorder(
      sorted.findIndex((clip) => clip.candidateId === source),
      sorted.findIndex((clip) => clip.candidateId === candidateId),
    );
  };

  if (!job) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center text-sm text-ink-400">
        Loading job…
      </div>
    );
  }

  const running = RUNNING.includes(job.state);

  return (
    <div className="space-y-6">
      {/* ---------------------------------------------------------- Header */}
      <Rise>
        <div className="relative overflow-hidden rounded-panel border border-line bg-paper p-5 shadow-card sm:p-7">
          <div
            aria-hidden
            className="pointer-events-none absolute -right-24 -top-28 size-72 rounded-full bg-azure-200/30 blur-3xl"
          />
          <div className="relative flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-2.5 flex flex-wrap items-center gap-2">
                <StatusBadge state={job.state} label={stateLabel(job.state)} />
                {connection !== "open" && running && (
                  <span className="text-xs text-ink-400">live updates {connection}</span>
                )}
              </div>
              <h1 className="text-xl font-semibold tracking-tight text-ink-900 sm:text-2xl">
                {job.progress.message || stateLabel(job.state)}
              </h1>
              {job.sources.length > 1 ? (
                <p className="mt-1.5 text-sm text-ink-500">
                  {job.sources.length} independently configured source videos
                </p>
              ) : job.video && (
                <p className="mt-1.5 text-sm text-ink-500">
                  {job.video.width}×{job.video.height} · {job.video.averageFrameRate} fps ·{" "}
                  {job.video.hasAudio ? "with audio" : "silent"}
                </p>
              )}
            </div>

            <div className="flex flex-wrap gap-2">
              <Link to="/jobs" className="no-underline">
                <Button variant="ghost" size="sm">
                  All jobs
                </Button>
              </Link>
              {running && (
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() =>
                    void api
                      .cancelJob(jobId)
                      .then(setJob)
                      .catch((caught) =>
                        setError(
                          caught instanceof ApiError ? caught.message : "Could not cancel.",
                        ),
                      )
                  }
                >
                  <IconX style={{ height: 14, width: 14 }} />
                  Cancel
                </Button>
              )}
              {(job.state === "failed" || job.state === "cancelled") && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    void api
                      .retryJob(jobId, newIdempotencyKey())
                      .then((next) => {
                        setJob(next);
                        setNotice("Requeued — it resumes from the last checkpoint.");
                      })
                      .catch((caught) =>
                        setError(caught instanceof ApiError ? caught.message : "Could not retry.")
                      )
                  }
                >
                  <IconRefresh style={{ height: 14, width: 14 }} />
                  Retry
                </Button>
              )}
            </div>
          </div>

          {running && (
            <div className="relative mt-5 space-y-1.5">
              <Progress value={job.progress.percent} />
              <p className="text-xs tabular-nums text-ink-400">
                {Math.round(job.progress.percent)}%
              </p>
            </div>
          )}

          {job.eligibleCount !== null && (
            <div className="relative mt-6 grid grid-cols-3 gap-3 border-t border-line pt-5">
              {[
                { label: "Requested", value: job.targetClipCount },
                { label: "Usable shots", value: job.eligibleCount },
                { label: "Selected", value: job.selectedCount },
              ].map((stat) => (
                <div key={stat.label}>
                  <p className="font-display text-2xl font-semibold tabular-nums text-ink-900">
                    {stat.value}
                  </p>
                  <p className="text-xs text-ink-400">{stat.label}</p>
                </div>
              ))}
            </div>
          )}

          {job.useGemini && (
            <p className="relative mt-4 text-xs text-ink-400">
              Gemini: {job.usage.requestsUsed} of {job.usage.requestCap} requests
              {job.usage.proxyVideoCandidates > 0 &&
                ` · ${job.usage.proxyVideoCandidates} proxy clip(s)`}
              {job.usage.cacheStatus !== "none" && ` · cache ${job.usage.cacheStatus}`}
            </p>
          )}
        </div>
      </Rise>

      {/* --------------------------------------------------------- Notices */}
      {job.error && (
        <Alert tone="rose" role="alert" title="This job failed">
          {job.error.message}
          {job.error.retryable && " You can retry it."}
        </Alert>
      )}
      {error && (
        <Alert tone="rose" role="alert">
          {error}
        </Alert>
      )}
      {notice && <Alert tone="azure">{notice}</Alert>}
      {job.partialResultReason && <Alert tone="amber">{job.partialResultReason}</Alert>}
      {job.warnings.map((warning) => (
        <Alert key={warning} tone="amber">
          <span className="flex gap-2">
            <IconAlert className="mt-0.5 flex-none" style={{ height: 14, width: 14 }} />
            {warningLabel(warning)}
          </span>
        </Alert>
      ))}
      {job.usage.fallbackReason && <Alert tone="neutral">{job.usage.fallbackReason}</Alert>}

      {/* ---------------------------------------------------------- Review */}
      {reviewReady && (
        <>
          <Rise delay={0.06}>
            <Panel className="py-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h2 className="text-base font-semibold tracking-tight text-ink-900">
                    Review · {clips.length} selected of {candidates.length}
                  </h2>
                  <p className="mt-0.5 text-xs text-ink-400">
                    Every source has its own section. Drag a selected clip onto another to reorder;
                    files are renumbered from 0001 at export.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {job.rankingEnabled ? (
                    <SegmentedControl options={ORDERS} value={order} onChange={setOrder} size="sm" />
                  ) : (
                    <span className="text-xs font-medium text-ink-400">Manual selection</span>
                  )}
                  <Button variant="outline" size="sm" onClick={() => void submit(clips)} loading={busy}>
                    Save trims
                  </Button>
                </div>
              </div>
            </Panel>
          </Rise>

          {candidates.length === 0 ? (
            <div className="rounded-panel border border-line bg-paper shadow-card">
              <EmptyState
                icon={<IconFilm style={{ height: 22, width: 22 }} />}
                title="No usable shots in this video"
                description={`No shot was long enough to produce a ${CLIP_RANGE_LABEL} second clip after transition guards were applied.`}
              />
            </div>
          ) : (
            <div className="space-y-7">
              {candidateGroups.map((group, groupIndex) => {
                const selectedInSource = group.candidates.filter((candidate) =>
                  clipByCandidate.has(candidate.id),
                ).length;
                return (
                  <Rise key={group.id} delay={Math.min(groupIndex * 0.04, 0.16)}>
                    <section className="rounded-panel border border-line bg-paper/60 p-4 shadow-soft sm:p-5">
                      <div className="mb-5 flex flex-wrap items-center justify-between gap-3 border-b border-line pb-4">
                        <div className="flex min-w-0 items-center gap-3">
                          <span className="grid size-10 flex-none place-items-center rounded-xl bg-navy-50 text-azure-700">
                            <IconLayers style={{ height: 18, width: 18 }} />
                          </span>
                          <div className="min-w-0">
                            <h3 className="truncate text-sm font-semibold text-ink-900">
                              {group.name}
                            </h3>
                            <p className="truncate text-xs text-ink-400">{group.fileName}</p>
                          </div>
                        </div>
                        <span className="rounded-full border border-line bg-canvas px-3 py-1 text-xs font-medium text-ink-500">
                          {selectedInSource} selected · {group.candidates.length} shot
                          {group.candidates.length === 1 ? "" : "s"}
                        </span>
                      </div>
                      <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
                        {group.candidates.map((candidate, index) => (
                          <Rise key={candidate.id} delay={Math.min(index * 0.025, 0.18)}>
                            <CandidateCard
                              candidate={candidate}
                              clip={clipByCandidate.get(candidate.id) ?? null}
                              order={clipByCandidate.get(candidate.id)?.order ?? null}
                              busy={busy}
                              onToggle={toggle}
                              onTrim={trim}
                              onRestore={restore}
                              onMove={move}
                              onDragStart={(id) => {
                                dragged.current = id;
                              }}
                              onDropOn={dropOn}
                            />
                          </Rise>
                        ))}
                      </div>
                    </section>
                  </Rise>
                );
              })}
            </div>
          )}

          <Rise delay={0.1}>
            <Panel>
              <ExportPanel
                job={job}
                selectedCount={clips.length}
                activeExport={activeExport}
                onStarted={setActiveExport}
              />
            </Panel>
          </Rise>
        </>
      )}

      {running && (
        <div className="rounded-panel border border-line bg-paper shadow-card">
          <EmptyState
            icon={<IconSparkle style={{ height: 22, width: 22 }} />}
            title="Analysis in progress"
            description="Detecting shot boundaries and measuring each candidate. You can close this tab — the job keeps running."
          />
        </div>
      )}
    </div>
  );
}
