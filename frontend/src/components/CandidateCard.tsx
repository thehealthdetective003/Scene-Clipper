import { useState } from "react";

import type { CandidateShot, SelectedClip } from "../api/types";
import {
  cacheStatusLabel,
  formatDuration,
  formatTimecode,
  scoringSourceLabel,
} from "../lib/format";
import { cn } from "../lib/cn";
import { Badge, Checkbox } from "./ui/Primitives";
import { IconAlert, IconArrowDown, IconArrowUp, IconPlay } from "./ui/Icons";
import { SpotlightCard } from "./ui/Spotlight";
import { TrimEditor } from "./TrimEditor";

interface Props {
  candidate: CandidateShot;
  clip: SelectedClip | null;
  order: number | null;
  busy: boolean;
  onToggle: (candidate: CandidateShot, selected: boolean) => void;
  onTrim: (candidateId: string, startUs: number, endUs: number) => void;
  onRestore: (candidate: CandidateShot) => void;
  onMove: (candidateId: string, direction: -1 | 1) => void;
  onDragStart: (candidateId: string) => void;
  onDropOn: (candidateId: string) => void;
}

export function CandidateCard({
  candidate,
  clip,
  order,
  busy,
  onToggle,
  onTrim,
  onRestore,
  onMove,
  onDragStart,
  onDropOn,
}: Props) {
  const [previewing, setPreviewing] = useState(false);
  const selected = clip !== null;
  const recommended = candidate.recommendedEndUs - candidate.recommendedStartUs;

  return (
    <SpotlightCard
      as="article"
      draggable={selected && !busy}
      onDragStart={() => onDragStart(candidate.id)}
      onDragOver={(event: React.DragEvent) => {
        if (selected) event.preventDefault();
      }}
      onDrop={(event: React.DragEvent) => {
        event.preventDefault();
        onDropOn(candidate.id);
      }}
      className={cn(
        "flex h-full flex-col p-4",
        selected && "border-azure-300 bg-azure-50/40 ring-1 ring-azure-200",
      )}
    >
      {/* Media */}
      <div className="relative mb-3 aspect-video overflow-hidden rounded-xl bg-navy-950">
        {previewing ? (
          <video
            className="size-full object-contain"
            src={candidate.previewUrl}
            controls
            autoPlay
            preload="metadata"
          />
        ) : (
          <button
            type="button"
            onClick={() => setPreviewing(true)}
            aria-label={`Play preview of shot ${candidate.shotNumber}`}
            className="group/play relative block size-full"
          >
            <img
              src={candidate.thumbnailUrl}
              alt=""
              loading="lazy"
              className="size-full object-cover transition-transform duration-500 group-hover/play:scale-105"
            />
            <span className="absolute inset-0 bg-gradient-to-t from-navy-950/70 via-transparent to-transparent" />
            <span className="absolute inset-0 grid place-items-center">
              <span className="grid size-11 place-items-center rounded-full bg-white/90 text-navy-900 shadow-lg backdrop-blur-sm transition-transform duration-300 group-hover/play:scale-110">
                <IconPlay style={{ height: 15, width: 15 }} className="ml-0.5" />
              </span>
            </span>
          </button>
        )}

        <span className="pointer-events-none absolute right-2 top-2 rounded-lg bg-navy-950/70 px-2 py-0.5 text-xs font-semibold tabular-nums text-white backdrop-blur-sm">
          {formatDuration(clip ? clip.endUs - clip.startUs : recommended)}
        </span>

        {order !== null && (
          <span className="pointer-events-none absolute left-2 top-2 grid size-7 place-items-center rounded-lg bg-azure-500 text-xs font-bold text-white shadow-azure">
            {order}
          </span>
        )}
      </div>

      {/* Header */}
      <div className="mb-3 flex items-start justify-between gap-2">
        <Checkbox
          checked={selected}
          disabled={busy}
          onChange={(next) => onToggle(candidate, next)}
          label={
            <span className="text-sm font-semibold text-ink-900">
              Shot {candidate.shotNumber}
              <span className="ml-1.5 text-xs font-normal text-ink-400">
                rank #{candidate.rank}
              </span>
            </span>
          }
        />

        {selected && (
          <div className="flex flex-none gap-1">
            <button
              type="button"
              onClick={() => onMove(candidate.id, -1)}
              disabled={busy}
              aria-label="Move earlier"
              className="grid size-7 place-items-center rounded-lg border border-line bg-white text-ink-500 transition-colors hover:border-azure-300 hover:text-navy-800 disabled:opacity-40"
            >
              <IconArrowUp style={{ height: 13, width: 13 }} />
            </button>
            <button
              type="button"
              onClick={() => onMove(candidate.id, 1)}
              disabled={busy}
              aria-label="Move later"
              className="grid size-7 place-items-center rounded-lg border border-line bg-white text-ink-500 transition-colors hover:border-azure-300 hover:text-navy-800 disabled:opacity-40"
            >
              <IconArrowDown style={{ height: 13, width: 13 }} />
            </button>
          </div>
        )}
      </div>

      {/* Meta */}
      <dl className="mb-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
        <div>
          <dt className="text-ink-400">Source</dt>
          <dd className="mt-0.5 font-medium tabular-nums text-ink-800">
            {formatTimecode(candidate.sourceStartUs)}
          </dd>
        </div>
        <div>
          <dt className="text-ink-400">Score</dt>
          <dd className="mt-0.5 font-medium tabular-nums text-ink-800">
            {candidate.score.toFixed(1)}
            <span className="ml-1 font-normal text-ink-400">
              · {candidate.confidence.toFixed(2)}
            </span>
          </dd>
        </div>
      </dl>

      <div className="mb-3 flex flex-wrap gap-1.5">
        <Badge tone={candidate.scoringSource === "local-fallback" ? "neutral" : "azure"}>
          {scoringSourceLabel(candidate.scoringSource)}
        </Badge>
        {candidate.cacheStatus !== "none" && (
          <Badge tone="neutral">{cacheStatusLabel(candidate.cacheStatus)}</Badge>
        )}
        {candidate.humanPresent === true && (
          <Badge tone="rose" dot>
            Person in shot
          </Badge>
        )}
        {candidate.productVisible === true && (
          <Badge tone="emerald" dot>
            Product{" "}
            {candidate.productProminence !== null && candidate.productProminence >= 70
              ? "hero"
              : "visible"}
          </Badge>
        )}
        {candidate.productVisible === false && <Badge tone="amber">No product</Badge>}
      </div>

      {/* Why this shot was passed over. It is still selectable by hand — the
          product-only rule is the default, not a lock. */}
      {candidate.excludedReason && (
        <p className="mb-2 flex gap-1.5 rounded-lg bg-warn-50 px-2.5 py-2 text-xs leading-relaxed text-warn-600">
          <IconAlert className="mt-0.5 flex-none" style={{ height: 12, width: 12 }} />
          {candidate.excludedReason === "human_present"
            ? `A person is visible${candidate.humanSource === "local" ? " (detected locally)" : ""}, so this shot was not selected automatically. You can still add it.`
            : "The product was not identified in this shot, so it was not selected automatically. You can still add it."}
        </p>
      )}

      {/* Model text is rendered as plain text by React; never interpreted as
          HTML, Markdown, a command, or a path (spec 10.3). */}
      {candidate.reason && (
        <p className="mb-2 text-xs leading-relaxed text-ink-500 [overflow-wrap:anywhere]">
          {candidate.reason}
        </p>
      )}

      {!candidate.promptRelevanceEvaluated && (
        <p className="text-xs text-ink-400">Prompt relevance was not evaluated for this clip.</p>
      )}

      <div className="flex-1" />

      {selected && clip && (
        <TrimEditor
          candidate={candidate}
          startUs={clip.startUs}
          endUs={clip.endUs}
          disabled={busy}
          onChange={(startUs, endUs) => onTrim(candidate.id, startUs, endUs)}
          onRestore={() => onRestore(candidate)}
        />
      )}
    </SpotlightCard>
  );
}
