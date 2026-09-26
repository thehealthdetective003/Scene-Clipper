import { useEffect, useState } from "react";

import type { CandidateShot } from "../api/types";
import { formatDuration, formatTimecode, usToSeconds } from "../lib/format";
import { cn } from "../lib/cn";
import {
  CLIP_RANGE_LABEL,
  MAX_CLIP_US,
  MIN_CLIP_US,
} from "../lib/clip";

/** One frame at 24fps, the coarsest common rate; the server re-quantizes exactly. */
const STEP_US = 41_667;

interface Props {
  candidate: CandidateShot;
  startUs: number;
  endUs: number;
  onChange: (startUs: number, endUs: number) => void;
  onRestore: () => void;
  disabled?: boolean;
}

/**
 * Constrained trim controls.
 *
 * The client keeps handles inside the safe interval and within the clip
 * duration bounds so illegal states are hard to reach, but the server
 * revalidates and frame-quantizes every submitted trim regardless (spec 5.6).
 */
export function TrimEditor({ candidate, startUs, endUs, onChange, onRestore, disabled }: Props) {
  const [start, setStart] = useState(startUs);
  const [end, setEnd] = useState(endUs);

  useEffect(() => {
    setStart(startUs);
    setEnd(endUs);
  }, [startUs, endUs]);

  const safeStart = candidate.safeStartUs;
  const safeEnd = candidate.safeEndUs;
  const span = Math.max(1, safeEnd - safeStart);
  const duration = end - start;

  const commit = (nextStart: number, nextEnd: number) => {
    setStart(nextStart);
    setEnd(nextEnd);
    onChange(nextStart, nextEnd);
  };

  const moveStart = (value: number) => {
    // Keep the clip legal by pushing the end along when the start advances.
    const nextStart = Math.max(safeStart, Math.min(value, safeEnd - MIN_CLIP_US));
    const minEnd = nextStart + MIN_CLIP_US;
    const maxEnd = Math.min(safeEnd, nextStart + MAX_CLIP_US);
    commit(nextStart, Math.max(minEnd, Math.min(end, maxEnd)));
  };

  const moveEnd = (value: number) => {
    const nextEnd = Math.min(safeEnd, Math.max(value, safeStart + MIN_CLIP_US));
    const minStart = Math.max(safeStart, nextEnd - MAX_CLIP_US);
    const maxStart = nextEnd - MIN_CLIP_US;
    commit(Math.max(minStart, Math.min(start, maxStart)), nextEnd);
  };

  const legal =
    duration >= MIN_CLIP_US && duration <= MAX_CLIP_US && start >= safeStart && end <= safeEnd;

  return (
    <div className="mt-4 space-y-3 border-t border-line pt-4">
      {/* Safe interval, with the selected region highlighted. */}
      <div className="relative h-2.5 overflow-hidden rounded-full bg-canvas-2">
        <div
          className={cn(
            "absolute inset-y-0 rounded-full transition-colors",
            legal
              ? "bg-gradient-to-r from-navy-700 via-azure-600 to-azure-400"
              : "bg-bad-500/80",
          )}
          style={{
            left: `${((start - safeStart) / span) * 100}%`,
            width: `${((end - start) / span) * 100}%`,
          }}
        />
      </div>

      <div className="space-y-2">
        <label className="grid grid-cols-[2.6rem_1fr_auto] items-center gap-3 text-xs">
          <span className="font-medium text-ink-400">Start</span>
          <input
            type="range"
            min={safeStart}
            max={Math.max(safeStart, safeEnd - MIN_CLIP_US)}
            step={STEP_US}
            value={start}
            disabled={disabled}
            onChange={(event) => moveStart(Number(event.target.value))}
          />
          <output className="font-medium tabular-nums text-ink-700">{formatTimecode(start)}</output>
        </label>

        <label className="grid grid-cols-[2.6rem_1fr_auto] items-center gap-3 text-xs">
          <span className="font-medium text-ink-400">End</span>
          <input
            type="range"
            min={Math.min(safeEnd, safeStart + MIN_CLIP_US)}
            max={safeEnd}
            step={STEP_US}
            value={end}
            disabled={disabled}
            onChange={(event) => moveEnd(Number(event.target.value))}
          />
          <output className="font-medium tabular-nums text-ink-700">{formatTimecode(end)}</output>
        </label>
      </div>

      <div className="flex items-center justify-between gap-3">
        <span
          className={cn(
            "rounded-full px-2.5 py-1 text-xs font-medium tabular-nums",
            legal
              ? "border border-azure-100 bg-azure-50 text-azure-800"
              : "border border-bad-100 bg-bad-50 text-bad-600",
          )}
        >
          {formatDuration(duration)}
          {!legal && ` — must be ${CLIP_RANGE_LABEL}s inside the safe interval`}
        </span>
        <button
          type="button"
          onClick={onRestore}
          disabled={disabled}
          className="text-xs font-semibold text-azure-700 transition-colors hover:text-azure-800 disabled:opacity-40"
        >
          Restore recommended
        </button>
      </div>

      <p className="text-xs leading-relaxed text-ink-400">
        Safe interval {formatTimecode(safeStart)} – {formatTimecode(safeEnd)} (
        {usToSeconds(safeEnd - safeStart).toFixed(2)}s usable). Handles cannot leave it, so a clip
        never crosses a cut, fade, or dissolve.
      </p>
    </div>
  );
}
