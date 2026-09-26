import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, api, newIdempotencyKey } from "../api/client";
import type { ExportFile, ExportRecord, Job, Resolution } from "../api/types";
import { formatBytes, formatDuration, stateLabel } from "../lib/format";
import { encodeSelection, selectionBytes } from "../lib/selection";
import { cn } from "../lib/cn";
import { Button, ButtonLink, ShimmerButton } from "./ui/Button";
import { IconCheck, IconDownload, IconFilm, IconRefresh, IconX } from "./ui/Icons";
import {
  Alert,
  Badge,
  Checkbox,
  Progress,
  SegmentedControl,
  StatusBadge,
  Switch,
} from "./ui/Primitives";

/**
 * Gap between browser-initiated downloads when saving a selection as separate
 * MP4s. Firing them in one burst makes Chrome drop all but the first few.
 */
const SEQUENTIAL_DOWNLOAD_GAP_MS = 600;

const RESOLUTIONS: Array<{ value: Resolution; label: string; hint: string }> = [
  { value: "original", label: "Original", hint: "Source display size" },
  { value: "max1080p", label: "Max 1080p", hint: "Fits inside 1920×1080" },
  { value: "max720p", label: "Max 720p", hint: "Fits inside 1280×720" },
];

interface Props {
  job: Job;
  selectedCount: number;
  activeExport: ExportRecord | null;
  onStarted: (record: ExportRecord) => void;
}

const RESOLUTION_LABELS: Record<Resolution, string> = {
  original: "Original",
  max1080p: "1080p",
  max720p: "720p",
};

export function ExportPanel({ job, selectedCount, activeExport, onStarted }: Props) {
  const [resolutions, setResolutions] = useState<Resolution[]>(["original"]);
  const [includeAudio, setIncludeAudio] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState<ExportFile[]>([]);
  const [zipUrl, setZipUrl] = useState<string | null>(null);
  const [filter, setFilter] = useState<Resolution | "all">("all");
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const running = activeExport?.state === "queued" || activeExport?.state === "exporting";
  const exportId = activeExport?.id;
  const complete = activeExport?.state === "complete";

  // Individual clips only exist once the export finishes.
  const loadFiles = useCallback(async () => {
    if (!exportId || !complete) {
      setFiles([]);
      setZipUrl(null);
      return;
    }
    try {
      const body = await api.exportFiles(job.id, exportId);
      setFiles(body.files);
      setZipUrl(body.zipDownloadUrl);
    } catch {
      setFiles([]);
    }
  }, [job.id, exportId, complete]);

  useEffect(() => {
    void loadFiles();
  }, [loadFiles]);

  // A new export replaces the file list, so any carried-over selection would
  // point at clips that no longer exist.
  useEffect(() => {
    setPicked(new Set());
    setSelecting(false);
  }, [exportId]);

  const shown = filter === "all" ? files : files.filter((file) => file.resolution === filter);
  const presentResolutions = [...new Set(files.map((file) => file.resolution))];

  // Only files that exist on disk can be bundled; older exports have rows but
  // no published MP4, and the server refuses a selection it cannot fulfil.
  const selectable = useMemo(() => shown.filter((file) => file.available), [shown]);
  const chosen = useMemo(() => files.filter((file) => picked.has(file.id)), [files, picked]);
  const allShownPicked = selectable.length > 0 && selectable.every((f) => picked.has(f.id));

  const togglePick = (fileId: string) =>
    setPicked((previous) => {
      const next = new Set(previous);
      if (next.has(fileId)) next.delete(fileId);
      else next.add(fileId);
      return next;
    });

  const toggleAllShown = () =>
    setPicked((previous) => {
      const next = new Set(previous);
      if (allShownPicked) selectable.forEach((file) => next.delete(file.id));
      else selectable.forEach((file) => next.add(file.id));
      return next;
    });

  const leaveSelectMode = () => {
    setSelecting(false);
    setPicked(new Set());
    setDownloadError(null);
  };

  /**
   * Hand a URL to the browser's download manager without navigating.
   *
   * A plain `location.href` would leave the app if the server answered with an
   * error instead of a file; an anchor with `download` never navigates, so the
   * page and the current selection survive whatever comes back.
   */
  const startDownload = (href: string, fileName?: string) => {
    const anchor = document.createElement("a");
    anchor.href = href;
    // Empty means "use the name the server sent" for a same-origin download.
    anchor.download = fileName ?? "";
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  };

  /**
   * The file list can go stale — retention may have swept the clips, or a new
   * export may have replaced them. The server refuses a selection it cannot
   * fulfil in full rather than sending a quietly incomplete ZIP, so the same
   * check is made here first to explain it properly.
   */
  const confirmStillAvailable = async (): Promise<boolean> => {
    if (!exportId) return false;
    const fresh = await api.exportFiles(job.id, exportId);
    const byId = new Map(fresh.files.map((file) => [file.id, file]));
    const missing = chosen.filter((file) => !byId.get(file.id)?.available);
    if (missing.length === 0) return true;

    setFiles(fresh.files);
    setPicked((previous) => {
      const next = new Set(previous);
      missing.forEach((file) => next.delete(file.id));
      return next;
    });
    setDownloadError(
      `${missing.length} selected clip${missing.length === 1 ? " is" : "s are"} no longer ` +
        "available and have been deselected. Re-export the job to get them back.",
    );
    return false;
  };

  const runDownload = async (action: () => Promise<void> | void) => {
    setSaving(true);
    setDownloadError(null);
    try {
      if (await confirmStillAvailable()) await action();
    } catch (caught) {
      setDownloadError(
        caught instanceof ApiError ? caught.message : "The download could not be started.",
      );
    } finally {
      setSaving(false);
    }
  };

  /** One ZIP containing exactly the chosen clips, streamed by the server. */
  const downloadChosenAsZip = () =>
    runDownload(() => {
      if (!exportId) return;
      startDownload(api.bundleUrl(job.id, exportId, encodeSelection(chosen)));
    });

  /**
   * Save each chosen clip as its own MP4. Browsers ask permission before the
   * second file and throttle bursts, so the links are spaced out.
   */
  const downloadChosenAsFiles = () =>
    runDownload(async () => {
      for (const [index, file] of chosen.entries()) {
        startDownload(file.downloadUrl, file.fileName);
        if (index < chosen.length - 1) {
          await new Promise((resolve) => setTimeout(resolve, SEQUENTIAL_DOWNLOAD_GAP_MS));
        }
      }
    });

  const toggle = (value: Resolution) =>
    setResolutions((previous) =>
      previous.includes(value) ? previous.filter((item) => item !== value) : [...previous, value],
    );

  const guard = async (action: () => Promise<ExportRecord>) => {
    setBusy(true);
    setError(null);
    try {
      onStarted(await action());
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The export request failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-base font-semibold tracking-tight text-ink-900">Export</h2>
        <p className="mt-1 text-sm text-ink-500">
          Frame-accurate H.264 clips. Download each one as an MP4, or take the whole
          set as a ZIP with JSON and CSV manifests.
        </p>
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        {RESOLUTIONS.map((option) => {
          const on = resolutions.includes(option.value);
          return (
            <button
              key={option.value}
              type="button"
              disabled={running || busy}
              onClick={() => toggle(option.value)}
              className={cn(
                "rounded-xl border p-3 text-left transition-all duration-200 disabled:opacity-50",
                on
                  ? "border-azure-400 bg-azure-50 shadow-[0_0_0_4px_rgb(0_163_250_/_0.10)]"
                  : "border-line bg-canvas hover:border-line-strong",
              )}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-ink-900">{option.label}</span>
                <span
                  className={cn(
                    "grid size-4 place-items-center rounded-full border transition-colors",
                    on ? "border-azure-500 bg-azure-500 text-white" : "border-line-strong",
                  )}
                >
                  {on && <IconCheck style={{ height: 10, width: 10 }} strokeWidth={3} />}
                </span>
              </span>
              <span className="mt-0.5 block text-xs text-ink-400">{option.hint}</span>
            </button>
          );
        })}
      </div>
      <p className="-mt-2 text-xs text-ink-400">Output never exceeds the source dimensions.</p>

      <div className="rounded-xl border border-line bg-canvas p-4">
        <Switch
          checked={includeAudio}
          disabled={running || busy}
          onChange={setIncludeAudio}
          label="Keep source audio"
          description={
            job.video && !job.video.hasAudio
              ? "This source has no audio track, so clips export silently."
              : "Both streams start at zero and stay synchronized."
          }
        />
      </div>

      {error && <Alert tone="rose" role="alert">{error}</Alert>}

      {activeExport && (
        <div className="rounded-xl border border-line bg-canvas p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <StatusBadge state={activeExport.state} label={stateLabel(activeExport.state)} />
            {running && (
              <Button
                variant="danger"
                size="sm"
                onClick={() => void guard(() => api.cancelExport(job.id, activeExport.id))}
              >
                Cancel export
              </Button>
            )}
          </div>

          {running && (
            <>
              <Progress value={activeExport.progress.percent} className="mb-2" />
              <p className="text-xs text-ink-400">{activeExport.progress.message}</p>
            </>
          )}

          {activeExport.state === "failed" && (
            <div className="space-y-3">
              <p className="text-sm text-bad-600">{activeExport.error?.message}</p>
              {activeExport.error?.retryable && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    void guard(() =>
                      api.retryExport(job.id, activeExport.id, newIdempotencyKey()),
                    )
                  }
                >
                  <IconRefresh style={{ height: 14, width: 14 }} />
                  Retry export
                </Button>
              )}
            </div>
          )}

        </div>
      )}

      {/* ------------------------------------------------- Finished clips */}
      {complete && files.length > 0 && (
        <div className="rounded-xl border border-line bg-canvas p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold text-ink-900">
                {files.length} file{files.length === 1 ? "" : "s"} ready
              </h3>
              <p className="text-xs text-ink-400">
                {selecting
                  ? "Tick the clips you want, then download them together."
                  : "Download any clip directly as MP4 — no unzipping needed."}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {presentResolutions.length > 1 && (
                <SegmentedControl
                  size="sm"
                  value={filter}
                  onChange={setFilter}
                  options={[
                    { value: "all" as const, label: "All" },
                    ...presentResolutions.map((r) => ({ value: r, label: RESOLUTION_LABELS[r] })),
                  ]}
                />
              )}
              {selecting ? (
                <Button variant="ghost" size="sm" onClick={leaveSelectMode}>
                  <IconX style={{ height: 14, width: 14 }} />
                  Done
                </Button>
              ) : (
                <Button variant="outline" size="sm" onClick={() => setSelecting(true)}>
                  <IconCheck style={{ height: 14, width: 14 }} />
                  Select
                </Button>
              )}
            </div>
          </div>

          {selecting && (
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-canvas px-3 py-2">
              <Checkbox
                checked={allShownPicked}
                disabled={selectable.length === 0}
                onChange={toggleAllShown}
                label={
                  <span className="text-xs font-medium text-ink-600">
                    {allShownPicked ? "Clear all" : `Select all ${selectable.length}`}
                    {filter !== "all" && " shown"}
                  </span>
                }
              />
              <span className="text-xs tabular-nums text-ink-400">
                {chosen.length} selected
                {chosen.length > 0 && ` · ${formatBytes(selectionBytes(chosen))}`}
              </span>
            </div>
          )}

          <ul className="divide-y divide-line">
            {shown.map((file) => {
              const isPicked = picked.has(file.id);
              return (
                <li
                  key={file.id}
                  className={cn(
                    "flex items-center gap-3 py-2.5 transition-colors",
                    selecting && file.available && "cursor-pointer",
                    isPicked && "bg-azure-50",
                  )}
                  onClick={
                    selecting && file.available ? () => togglePick(file.id) : undefined
                  }
                >
                  {selecting ? (
                    <span className="flex-none" onClick={(event) => event.stopPropagation()}>
                      <Checkbox
                        checked={isPicked}
                        disabled={!file.available}
                        onChange={() => togglePick(file.id)}
                        label=""
                      />
                    </span>
                  ) : (
                    <span className="grid size-9 flex-none place-items-center rounded-lg border border-line bg-canvas-2 text-azure-700">
                      <IconFilm style={{ height: 16, width: 16 }} />
                    </span>
                  )}

                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-ink-900">{file.fileName}</p>
                    <p className="text-xs text-ink-400">
                      {file.width}×{file.height} · {formatDuration(file.durationUs)} ·{" "}
                      {formatBytes(file.sizeBytes)}
                    </p>
                  </div>

                  <Badge tone="neutral" className="hidden sm:inline-flex">
                    {RESOLUTION_LABELS[file.resolution]}
                  </Badge>

                  {file.available ? (
                    !selecting && (
                      <ButtonLink
                        href={file.downloadUrl}
                        download={file.fileName}
                        variant="outline"
                        size="sm"
                        className="flex-none"
                      >
                        <IconDownload style={{ height: 14, width: 14 }} />
                        MP4
                      </ButtonLink>
                    )
                  ) : (
                    <span
                      className="flex-none text-xs text-ink-400"
                      title="This export predates individual downloads. Re-export to get MP4s."
                    >
                      in ZIP only
                    </span>
                  )}
                </li>
              );
            })}
          </ul>

          {selecting && downloadError && (
            <Alert tone="rose" className="mt-3" role="alert">
              {downloadError}
            </Alert>
          )}

          {selecting && (
            <div className="mt-3 flex flex-wrap gap-2 border-t border-line pt-3">
              <Button
                variant="primary"
                size="sm"
                loading={saving}
                onClick={() => void downloadChosenAsZip()}
                disabled={chosen.length === 0 || saving}
              >
                <IconDownload style={{ height: 14, width: 14 }} />
                Download {chosen.length || ""} together
                {chosen.length > 0 && ` (${formatBytes(selectionBytes(chosen))})`}
              </Button>
              <Button
                variant="outline"
                size="sm"
                loading={saving}
                onClick={() => void downloadChosenAsFiles()}
                disabled={chosen.length === 0 || saving}
              >
                Save as separate MP4s
              </Button>
            </div>
          )}

          {selecting && chosen.length > 0 && (
            <p className="mt-2 text-xs text-ink-400">
              “Together” gives you one ZIP of the {chosen.length} selected clip
              {chosen.length === 1 ? "" : "s"} with matching manifests. “Separate MP4s” saves
              each file on its own — your browser will ask permission to save several files.
            </p>
          )}

          {!selecting && zipUrl && (
            <div className="mt-3 border-t border-line pt-3">
              <a
                href={zipUrl}
                className="inline-flex items-center gap-1.5 text-xs font-medium text-ink-500 no-underline transition-colors hover:text-ink-900"
              >
                <IconDownload style={{ height: 13, width: 13 }} />
                Or download everything as one ZIP (includes JSON + CSV manifests)
              </a>
            </div>
          )}
        </div>
      )}

      <ShimmerButton
        onClick={() =>
          void guard(() =>
            api.createExport(
              job.id,
              job.reviewRevision,
              resolutions,
              includeAudio,
              newIdempotencyKey(),
            ),
          )
        }
        disabled={running || busy || resolutions.length === 0 || selectedCount === 0}
        className="w-full"
      >
        {busy
          ? "Starting…"
          : `Export ${selectedCount} clip${selectedCount === 1 ? "" : "s"}`}
      </ShimmerButton>

      {selectedCount === 0 && (
        <p className="text-center text-xs text-ink-400">Select at least one clip to export.</p>
      )}
    </div>
  );
}
