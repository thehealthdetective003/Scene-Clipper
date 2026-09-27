import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, api, newIdempotencyKey } from "../api/client";
import type { ExportFile, ExportRecord, Job, Resolution, SourceBundle } from "../api/types";
import { cn } from "../lib/cn";
import { formatBytes, formatDuration, stateLabel } from "../lib/format";
import { encodeSelection, selectionBytes } from "../lib/selection";
import { Button, ButtonLink, ShimmerButton } from "./ui/Button";
import {
  IconArchive,
  IconCheck,
  IconDownload,
  IconFilm,
  IconFolder,
  IconLayers,
  IconRefresh,
  IconX,
} from "./ui/Icons";
import {
  Alert,
  Badge,
  Checkbox,
  Input,
  Progress,
  SegmentedControl,
  StatusBadge,
  Switch,
} from "./ui/Primitives";

const SEQUENTIAL_DOWNLOAD_GAP_MS = 600;

const RESOLUTIONS: Array<{ value: Resolution; label: string; hint: string }> = [
  { value: "original", label: "Original", hint: "Source display size" },
  { value: "max1080p", label: "Max 1080p", hint: "Fits inside 1920x1080" },
  { value: "max720p", label: "Max 720p", hint: "Fits inside 1280x720" },
];

const RESOLUTION_LABELS: Record<Resolution, string> = {
  original: "Original",
  max1080p: "1080p",
  max720p: "720p",
};

interface Props {
  job: Job;
  selectedCount: number;
  activeExport: ExportRecord | null;
  onStarted: (record: ExportRecord) => void;
}

function initialZipName(job: Job): string {
  const sources = job.sources ?? [];
  const first = sources[0];
  const sourceName = first?.sourceName || first?.fileName.replace(/\.[^.]+$/, "");
  return `${sources.length === 1 && sourceName ? sourceName : "scene-clips"}.zip`;
}

export function ExportPanel({ job, selectedCount, activeExport, onStarted }: Props) {
  const [resolutions, setResolutions] = useState<Resolution[]>(["original"]);
  const [includeAudio, setIncludeAudio] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState<ExportFile[]>([]);
  const [sourceBundles, setSourceBundles] = useState<SourceBundle[]>([]);
  const [zipUrl, setZipUrl] = useState<string | null>(null);
  const [zipName, setZipName] = useState(() => initialZipName(job));
  const [filter, setFilter] = useState<Resolution | "all">("all");
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloadNotice, setDownloadNotice] = useState<string | null>(null);
  const [saveProgress, setSaveProgress] = useState<string | null>(null);

  const running = activeExport?.state === "queued" || activeExport?.state === "exporting";
  const exportId = activeExport?.id;
  const complete = activeExport?.state === "complete";
  const currentExport = complete && activeExport.reviewRevision === job.reviewRevision;
  const jobSources = job.sources ?? [];

  const loadFiles = useCallback(async () => {
    if (!exportId || !complete) {
      setFiles([]);
      setSourceBundles([]);
      setZipUrl(null);
      return;
    }
    try {
      const body = await api.exportFiles(job.id, exportId);
      setFiles(body.files);
      setSourceBundles(body.sourceBundles);
      setZipUrl(body.zipDownloadUrl);
    } catch {
      setFiles([]);
      setSourceBundles([]);
    }
  }, [job.id, exportId, complete]);

  useEffect(() => {
    void loadFiles();
  }, [loadFiles]);

  useEffect(() => {
    setPicked(new Set());
    setSelecting(false);
    setZipName(initialZipName(job));
    setDownloadError(null);
    setDownloadNotice(null);
    setSaveProgress(null);
  }, [exportId]);

  const shown = filter === "all" ? files : files.filter((file) => file.resolution === filter);
  const presentResolutions = [...new Set(files.map((file) => file.resolution))];
  const selectable = useMemo(() => shown.filter((file) => file.available), [shown]);
  const availableFiles = useMemo(() => files.filter((file) => file.available), [files]);
  const chosen = useMemo(() => files.filter((file) => picked.has(file.id)), [files, picked]);
  const downloadFiles = selecting ? chosen : availableFiles;
  const allShownPicked = selectable.length > 0 && selectable.every((file) => picked.has(file.id));

  const fileGroups = useMemo(() => {
    if (jobSources.length <= 1) {
      return [{ id: jobSources[0]?.id ?? "source", name: null, files: shown }];
    }
    return jobSources
      .map((source) => ({
        id: source.id,
        name: source.sourceName || source.fileName,
        files: shown.filter((file) => file.sourceId === source.id),
      }))
      .filter((group) => group.files.length > 0);
  }, [jobSources, shown]);

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
    setDownloadNotice(null);
  };

  const startDownload = (href: string, fileName?: string) => {
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = fileName ?? "";
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  };

  const confirmStillAvailable = async (requested: ExportFile[]): Promise<boolean> => {
    if (!exportId) return false;
    const fresh = await api.exportFiles(job.id, exportId);
    const byId = new Map(fresh.files.map((file) => [file.id, file]));
    const missing = requested.filter((file) => !byId.get(file.id)?.available);
    if (missing.length === 0) return true;

    setFiles(fresh.files);
    setSourceBundles(fresh.sourceBundles);
    setPicked((previous) => {
      const next = new Set(previous);
      missing.forEach((file) => next.delete(file.id));
      return next;
    });
    setDownloadError(
      `${missing.length} selected clip${missing.length === 1 ? " is" : "s are"} no longer ` +
        "available. Re-export the job to restore them.",
    );
    return false;
  };

  const runDownload = async (requested: ExportFile[], action: () => Promise<void> | void) => {
    setSaving(true);
    setDownloadError(null);
    setDownloadNotice(null);
    try {
      if (await confirmStillAvailable(requested)) await action();
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") return;
      setDownloadError(
        caught instanceof Error ? caught.message : "The download could not be started.",
      );
    } finally {
      setSaving(false);
      setSaveProgress(null);
    }
  };

  const downloadAsZip = () => {
    if (!exportId || !zipName.trim()) return;
    if (!selecting) {
      startDownload(api.downloadUrl(job.id, exportId, zipName), zipName);
      return;
    }
    void runDownload(downloadFiles, () => {
      startDownload(
        api.bundleUrl(job.id, exportId, encodeSelection(downloadFiles), zipName),
        zipName,
      );
    });
  };

  const saveAsIndividualFiles = async () => {
    setSaving(true);
    setDownloadError(null);
    setDownloadNotice(null);
    try {
      // The picker must be opened directly from the button gesture. Waiting on
      // the availability request first would make Chromium reject it.
      const directory = window.showDirectoryPicker
        ? await window.showDirectoryPicker({ mode: "readwrite" })
        : null;
      if (!(await confirmStillAvailable(downloadFiles))) return;

      if (!directory) {
        setDownloadNotice(
          "Folder selection is unavailable in this browser, so the files were sent to its normal download folder.",
        );
        for (const [index, file] of downloadFiles.entries()) {
          startDownload(file.downloadUrl, file.fileName);
          if (index < downloadFiles.length - 1) {
            await new Promise((resolve) => setTimeout(resolve, SEQUENTIAL_DOWNLOAD_GAP_MS));
          }
        }
        return;
      }

      for (const [index, file] of downloadFiles.entries()) {
        setSaveProgress(`Saving ${index + 1} of ${downloadFiles.length}: ${file.fileName}`);
        const response = await fetch(file.downloadUrl, { credentials: "same-origin" });
        if (!response.ok) throw new Error(`Could not download ${file.fileName}.`);
        const handle = await directory.getFileHandle(file.fileName, { create: true });
        const writable = await handle.createWritable();
        if (response.body) await response.body.pipeTo(writable);
        else {
          await writable.write(await response.blob());
          await writable.close();
        }
      }
      setDownloadNotice(
        `${downloadFiles.length} MP4${downloadFiles.length === 1 ? "" : "s"} saved to the selected folder.`,
      );
    } catch (caught) {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) {
        setDownloadError(
          caught instanceof Error ? caught.message : "The download could not be started.",
        );
      }
    } finally {
      setSaving(false);
      setSaveProgress(null);
    }
  };

  const downloadAllSourceBundles = async () => {
    if (!exportId || sourceBundles.some((source) => !source.available)) return;
    setSaving(true);
    setDownloadError(null);
    try {
      for (const [index, source] of sourceBundles.entries()) {
        startDownload(
          api.sourceBundleUrl(job.id, exportId, source.sourceId, source.fileName),
          source.fileName,
        );
        if (index < sourceBundles.length - 1) {
          await new Promise((resolve) => setTimeout(resolve, SEQUENTIAL_DOWNLOAD_GAP_MS));
        }
      }
    } finally {
      setSaving(false);
    }
  };

  const downloadOneSourceBundle = (source: SourceBundle) => {
    if (!exportId) return;
    startDownload(
      api.sourceBundleUrl(job.id, exportId, source.sourceId, source.fileName),
      source.fileName,
    );
  };

  const toggleResolution = (value: Resolution) =>
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
          Create frame-accurate H.264 clips, then save individual MP4s or organized ZIP archives.
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
              onClick={() => toggleResolution(option.value)}
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
          description="Video and audio streams start at zero and remain synchronized."
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
                    void guard(() => api.retryExport(job.id, activeExport.id, newIdempotencyKey()))
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

      {currentExport && (files.length > 0 || zipUrl) && (
        <div className="rounded-xl border border-line bg-canvas p-4">
          {files.length > 0 && (
            <>
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-ink-900">
                    {files.length} file{files.length === 1 ? "" : "s"} ready
                  </h3>
                  <p className="text-xs text-ink-400">
                    {selecting
                      ? "Tick any clips you want to download as a smaller set."
                      : "Files are grouped by source and can also be downloaded directly."}
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
                        ...presentResolutions.map((value) => ({
                          value,
                          label: RESOLUTION_LABELS[value],
                        })),
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
                      Select clips
                    </Button>
                  )}
                </div>
              </div>

              {selecting && (
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-paper px-3 py-2">
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

              <div className="space-y-4">
                {fileGroups.map((group) => (
                  <section key={group.id}>
                    {group.name && (
                      <div className="mb-1.5 flex items-center gap-2 rounded-lg bg-paper px-3 py-2">
                        <IconLayers className="text-azure-700" style={{ height: 14, width: 14 }} />
                        <h4 className="truncate text-xs font-semibold uppercase tracking-wider text-ink-600">
                          {group.name}
                        </h4>
                        <span className="ml-auto text-xs text-ink-400">{group.files.length} files</span>
                      </div>
                    )}
                    <ul className="divide-y divide-line">
                      {group.files.map((file) => {
                        const isPicked = picked.has(file.id);
                        return (
                          <li
                            key={file.id}
                            className={cn(
                              "flex items-center gap-3 px-1 py-2.5 transition-colors",
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
                              <p className="truncate text-sm font-medium text-ink-900">
                                {file.fileName}
                              </p>
                              <p className="text-xs text-ink-400">
                                {file.width}x{file.height} · {formatDuration(file.durationUs)} ·{" "}
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
                              <span className="flex-none text-xs text-ink-400">ZIP only</span>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  </section>
                ))}
              </div>
            </>
          )}

          {downloadError && <Alert tone="rose" className="mt-3" role="alert">{downloadError}</Alert>}
          {downloadNotice && <Alert tone="emerald" className="mt-3" role="status">{downloadNotice}</Alert>}

          <div className={cn("border-line pt-5", files.length > 0 && "mt-5 border-t")}>
            <div className="mb-4">
              <h3 className="text-sm font-semibold text-ink-900">
                {selecting ? `Download ${chosen.length} selected` : "Download all clips"}
              </h3>
              <p className="mt-1 text-xs text-ink-400">
                Choose individual MP4 files or one named ZIP archive.
              </p>
            </div>

            <div className="grid gap-3 lg:grid-cols-2">
              <div className="flex flex-col rounded-xl border border-line bg-paper p-4 shadow-soft">
                <DownloadOptionHeading
                  icon={<IconFolder style={{ height: 18, width: 18 }} />}
                  title="Individual MP4 files"
                  description="Pick a folder in Explorer and save every clip directly into it."
                />
                <Button
                  variant="outline"
                  size="md"
                  loading={saving}
                  onClick={() => void saveAsIndividualFiles()}
                  disabled={downloadFiles.length === 0 || saving}
                  className="mt-4 w-full"
                >
                  <IconFolder style={{ height: 15, width: 15 }} />
                  Choose folder & save {downloadFiles.length} MP4
                  {downloadFiles.length === 1 ? "" : "s"}
                </Button>
                {saveProgress && <p className="mt-2 truncate text-xs text-azure-700" role="status">{saveProgress}</p>}
              </div>

              <div className="flex flex-col rounded-xl border border-line bg-paper p-4 shadow-soft">
                <DownloadOptionHeading
                  icon={<IconArchive style={{ height: 18, width: 18 }} />}
                  title="One ZIP archive"
                  description="Includes the clips plus matching JSON and CSV manifests."
                />
                <label className="mt-4 block">
                  <span className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-ink-400">
                    ZIP file name
                  </span>
                  <Input
                    value={zipName}
                    maxLength={120}
                    onChange={(event) => setZipName(event.target.value)}
                    aria-label="ZIP file name"
                    placeholder="scene-clips.zip"
                  />
                </label>
                <Button
                  variant="primary"
                  size="md"
                  onClick={downloadAsZip}
                  disabled={
                    !zipName.trim() ||
                    (selecting && downloadFiles.length === 0) ||
                    (!selecting && !zipUrl)
                  }
                  className="mt-3 w-full"
                >
                  <IconDownload style={{ height: 15, width: 15 }} />
                  Download ZIP
                  {downloadFiles.length > 0 && ` · ${formatBytes(selectionBytes(downloadFiles))}`}
                </Button>
              </div>
            </div>

            {!selecting && jobSources.length > 1 && sourceBundles.length > 0 && (
              <div className="mt-4 rounded-xl border border-line bg-paper p-4 shadow-soft">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <DownloadOptionHeading
                    icon={<IconLayers style={{ height: 18, width: 18 }} />}
                    title="Separate ZIP for each source"
                    description="Each archive is named after its source and contains only that video's clips."
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    loading={saving}
                    onClick={() => void downloadAllSourceBundles()}
                    disabled={saving || sourceBundles.some((source) => !source.available)}
                  >
                    <IconDownload style={{ height: 14, width: 14 }} />
                    Download all source ZIPs
                  </Button>
                </div>
                <ul className="mt-4 divide-y divide-line border-t border-line">
                  {sourceBundles.map((source) => (
                    <li key={source.sourceId} className="flex items-center gap-3 py-3">
                      <span className="grid size-8 flex-none place-items-center rounded-lg bg-canvas text-azure-700">
                        <IconArchive style={{ height: 15, width: 15 }} />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-ink-900">{source.fileName}</p>
                        <p className="truncate text-xs text-ink-400">
                          {source.sourceName || source.sourceFileName} · {source.fileCount} file
                          {source.fileCount === 1 ? "" : "s"} · {formatBytes(source.sizeBytes)}
                        </p>
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => downloadOneSourceBundle(source)}
                        disabled={!source.available}
                      >
                        <IconDownload style={{ height: 13, width: 13 }} />
                        ZIP
                      </Button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}

      {!currentExport && (
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
          {busy ? "Starting..." : `Export ${selectedCount} clip${selectedCount === 1 ? "" : "s"}`}
        </ShimmerButton>
      )}

      {selectedCount === 0 && (
        <p className="text-center text-xs text-ink-400">Select at least one clip to export.</p>
      )}
    </div>
  );
}

function DownloadOptionHeading({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <span className="grid size-10 flex-none place-items-center rounded-xl bg-azure-50 text-azure-700">
        {icon}
      </span>
      <div>
        <h4 className="text-sm font-semibold text-ink-900">{title}</h4>
        <p className="mt-1 text-xs leading-relaxed text-ink-400">{description}</p>
      </div>
    </div>
  );
}
