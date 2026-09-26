import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { CLIP_RANGE_LABEL } from "../lib/clip";
import { ApiError, api } from "../api/client";
import type { JobSummary } from "../api/types";
import { Button } from "../components/ui/Button";
import { IconFilm, IconPlus, IconTrash } from "../components/ui/Icons";
import {
  Alert,
  Badge,
  EmptyState,
  Progress,
  Rise,
  SegmentedControl,
  StatusBadge,
} from "../components/ui/Primitives";
import { SpotlightCard } from "../components/ui/Spotlight";
import { stateLabel } from "../lib/format";

type Filter = "all" | "active" | "ready" | "failed";

const FILTERS = [
  { value: "all", label: "All" },
  { value: "active", label: "Running" },
  { value: "ready", label: "Ready" },
  { value: "failed", label: "Issues" },
] as const;

const ACTIVE = new Set(["uploaded", "probing", "detecting", "ranking", "exporting"]);

export function JobsPage() {
  const navigate = useNavigate();
  const [items, setItems] = useState<JobSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const load = async (next?: string) => {
    setLoading(true);
    try {
      const page = await api.listJobs(next);
      setItems((previous) => (next ? [...previous, ...page.items] : page.items));
      setCursor(page.nextCursor);
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load jobs.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  // Poll while anything is in flight so the list reflects worker progress.
  useEffect(() => {
    if (!items.some((job) => ACTIVE.has(job.state))) return;
    const timer = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(timer);
  }, [items]);

  const visible = useMemo(() => {
    switch (filter) {
      case "active":
        return items.filter((job) => ACTIVE.has(job.state));
      case "ready":
        return items.filter((job) => job.state === "review-ready" || job.state === "complete");
      case "failed":
        return items.filter((job) => job.state === "failed" || job.state === "cancelled");
      default:
        return items;
    }
  }, [items, filter]);

  const remove = async (event: React.MouseEvent, jobId: string) => {
    event.preventDefault();
    event.stopPropagation();
    if (!window.confirm("Delete this job and all of its generated files? This cannot be undone.")) {
      return;
    }
    try {
      await api.deleteJob(jobId);
      setItems((previous) => previous.filter((item) => item.id !== jobId));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not delete the job.");
    }
  };

  return (
    <div className="space-y-7">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[1.75rem] font-semibold tracking-tight text-ink-900 sm:text-[2rem]">
            Jobs
          </h1>
          <p className="mt-1.5 text-sm text-ink-500">
            {items.length === 0
              ? "Every analysis you run appears here."
              : `${items.length} job${items.length === 1 ? "" : "s"} · sources and exports stay until you delete them.`}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <SegmentedControl
            options={FILTERS}
            value={filter}
            onChange={setFilter}
            size="sm"
            className="hidden sm:inline-flex"
          />
          <Button variant="primary" onClick={() => navigate("/new")}>
            <IconPlus style={{ height: 16, width: 16 }} />
            New job
          </Button>
        </div>
      </div>

      {error && (
        <Alert tone="rose" role="alert">
          {error}
        </Alert>
      )}

      {!loading && visible.length === 0 && (
        <div className="rounded-panel border border-line bg-paper shadow-card">
          <EmptyState
            icon={<IconFilm style={{ height: 22, width: 22 }} />}
            title={items.length === 0 ? "No jobs yet" : "Nothing matches this filter"}
            description={
              items.length === 0
                ? `Upload one long video and Scene Clipper will find its best ${CLIP_RANGE_LABEL} second shots.`
                : "Try a different filter to see your other jobs."
            }
            action={
              items.length === 0 ? (
                <Button variant="primary" className="mt-2" onClick={() => navigate("/new")}>
                  <IconPlus style={{ height: 16, width: 16 }} />
                  Start a job
                </Button>
              ) : undefined
            }
          />
        </div>
      )}

      <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
        {visible.map((job, index) => (
          <Rise key={job.id} delay={Math.min(index * 0.04, 0.3)}>
            <Link to={`/jobs/${job.id}`} className="block no-underline">
              <SpotlightCard className="h-full p-5">
                <div className="mb-4 flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <span className="grid size-11 flex-none place-items-center rounded-xl bg-gradient-to-br from-navy-900 to-azure-600 text-white shadow-navy">
                      <IconFilm style={{ height: 19, width: 19 }} />
                    </span>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-ink-900">
                        {job.sourceFileName}
                      </p>
                      <p className="mt-0.5 text-xs text-ink-400">
                        {new Date(job.createdAt).toLocaleString()}
                      </p>
                    </div>
                  </div>
                  <StatusBadge state={job.state} label={stateLabel(job.state)} />
                </div>

                {ACTIVE.has(job.state) && (
                  <div className="mb-4 space-y-1.5">
                    <Progress value={job.progressPercent} />
                    <p className="text-xs tabular-nums text-ink-400">
                      {Math.round(job.progressPercent)}% complete
                    </p>
                  </div>
                )}

                <div className="flex items-end justify-between gap-3 border-t border-line pt-4">
                  <div>
                    <p className="font-display text-2xl font-semibold tabular-nums text-ink-900">
                      {job.selectedCount}
                      <span className="text-base font-normal text-ink-300">
                        {" / "}
                        {job.targetClipCount}
                      </span>
                    </p>
                    <p className="text-xs text-ink-400">clips selected</p>
                  </div>

                  <div className="flex items-center gap-2">
                    {job.latestExport?.state === "complete" && (
                      <Badge tone="emerald" dot>
                        Export ready
                      </Badge>
                    )}
                    <button
                      type="button"
                      aria-label={`Delete job for ${job.sourceFileName}`}
                      onClick={(event) => void remove(event, job.id)}
                      className="grid size-8 place-items-center rounded-full text-ink-300 transition-colors hover:bg-bad-50 hover:text-bad-600"
                    >
                      <IconTrash style={{ height: 15, width: 15 }} />
                    </button>
                  </div>
                </div>
              </SpotlightCard>
            </Link>
          </Rise>
        ))}
      </div>

      {cursor && (
        <div className="flex justify-center pt-2">
          <Button variant="outline" onClick={() => void load(cursor)} loading={loading}>
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}
