import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, api, newIdempotencyKey } from "../api/client";
import type { JobSourceInput, Upload } from "../api/types";
import { Button, ShimmerButton } from "../components/ui/Button";
import {
  IconDownload,
  IconFilm,
  IconLink,
  IconPlus,
  IconSparkle,
  IconUpload,
  IconX,
} from "../components/ui/Icons";
import {
  Alert,
  Field,
  Input,
  Panel,
  Progress,
  Rise,
  Switch,
  Textarea,
} from "../components/ui/Primitives";
import { GlowBorder } from "../components/ui/Spotlight";
import { MAX_CLIP_SECONDS, MIN_CLIP_SECONDS } from "../lib/clip";
import { formatBytes } from "../lib/format";
import {
  ResumableUpload,
  UploadCancelled,
  type UploadProgress,
  waitForVerification,
} from "../lib/uploader";

const MAX_PROMPT_CHARS = 2000;
const MAX_SOURCE_NAME_CHARS = 48;
const MAX_SOURCES = 12;
const ACCEPTED = ".mp4,.mov,.mkv,.webm,video/*";
type SourceKind = "file" | "url";
type SourceStatus = "empty" | "preparing" | "ready" | "failed";

interface SourceRow {
  key: string;
  kind: SourceKind;
  url: string;
  preparedUrl: string | null;
  chosenFileName: string | null;
  upload: Upload | null;
  transferUpload: Upload | null;
  progress: UploadProgress | null;
  sourceName: string;
  sourceNameTouched: boolean;
  contentPrompt: string;
  status: SourceStatus;
  error: string | null;
}

interface ValidationField {
  field?: unknown;
  message?: unknown;
}

function createSource(kind: SourceKind): SourceRow {
  return {
    key: newIdempotencyKey(),
    kind,
    url: "",
    preparedUrl: null,
    chosenFileName: null,
    upload: null,
    transferUpload: null,
    progress: null,
    sourceName: "",
    sourceNameTouched: false,
    contentPrompt: "",
    status: "empty",
    error: null,
  };
}

function validVideoUrl(value: string): boolean {
  try {
    const parsed = new URL(value.trim());
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function sourceIsReady(source: SourceRow): boolean {
  if (source.status !== "ready" || !source.upload) return false;
  return source.kind === "file" || source.preparedUrl === source.url.trim();
}

function sourceStatus(source: SourceRow): string {
  if (source.status === "preparing") {
    return source.kind === "url" ? "Downloading" : "Uploading";
  }
  if (sourceIsReady(source)) return "Ready";
  if (source.status === "failed") return "Needs attention";
  if (source.kind === "url" && source.upload) return "Link changed";
  return source.kind === "url" ? "Add link" : "Choose file";
}

function jobCreationError(caught: unknown): string {
  if (!(caught instanceof ApiError)) return "The job could not be created.";
  const fields = caught.details.fields;
  if (!Array.isArray(fields) || fields.length === 0) return caught.message;

  const first = fields[0] as ValidationField;
  const field = typeof first.field === "string" ? first.field : "";
  const rawMessage = typeof first.message === "string" ? first.message : caught.message;
  const message = rawMessage.replace(/^Value error,\s*/i, "");
  const sourceMatch = field.match(
    /(?:^|\.)sources\.(\d+)\.(sourceName|source_name|contentPrompt|content_prompt)$/,
  );
  if (sourceMatch) {
    const sourceNumber = Number(sourceMatch[1]) + 1;
    const label = sourceMatch[2]?.toLowerCase().includes("name") ? "name" : "instruction";
    return `Source ${sourceNumber} ${label}: ${message}`;
  }
  return message;
}

export function NewJobPage() {
  const navigate = useNavigate();
  const [sources, setSources] = useState<SourceRow[]>(() => [createSource("url")]);
  const [draggingKey, setDraggingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [rankingEnabled, setRankingEnabled] = useState(false);
  const [useGemini, setUseGemini] = useState(false);
  const [targetClipCount, setTargetClipCount] = useState(20);

  const operations = useRef(new Map<string, string>());
  const uploaders = useRef(new Map<string, ResumableUpload>());
  const remoteUploads = useRef(new Map<string, string>());

  const updateSource = (key: string, change: Partial<SourceRow>) =>
    setSources((current) =>
      current.map((source) => (source.key === key ? { ...source, ...change } : source)),
    );

  const updateIfCurrent = (key: string, operationId: string, change: Partial<SourceRow>) => {
    if (operations.current.get(key) !== operationId) return;
    updateSource(key, change);
  };

  const finishOperation = (key: string, operationId: string) => {
    if (operations.current.get(key) !== operationId) return;
    operations.current.delete(key);
    uploaders.current.delete(key);
    remoteUploads.current.delete(key);
  };

  const addSource = (kind: SourceKind) => {
    setError(null);
    setSources((current) =>
      current.length < MAX_SOURCES ? [...current, createSource(kind)] : current,
    );
  };

  const cancelPreparation = async (source: SourceRow) => {
    operations.current.delete(source.key);
    uploaders.current.get(source.key)?.cancel();
    uploaders.current.delete(source.key);

    const pendingId = source.transferUpload?.id ?? remoteUploads.current.get(source.key);
    remoteUploads.current.delete(source.key);
    updateSource(source.key, {
      transferUpload: null,
      progress: null,
      status: source.upload ? "ready" : "empty",
      error: null,
    });
    if (pendingId && pendingId !== source.upload?.id) {
      await api.deleteUpload(pendingId).catch(() => undefined);
    }
  };

  const removeSource = async (source: SourceRow) => {
    operations.current.delete(source.key);
    uploaders.current.get(source.key)?.cancel();
    uploaders.current.delete(source.key);
    remoteUploads.current.delete(source.key);
    setSources((current) => current.filter((item) => item.key !== source.key));

    const ids = new Set(
      [source.upload?.id, source.transferUpload?.id].filter(
        (value): value is string => typeof value === "string",
      ),
    );
    await Promise.all([...ids].map((id) => api.deleteUpload(id).catch(() => undefined)));
  };

  const prepareFile = async (source: SourceRow, file: File) => {
    if (source.status === "preparing") return;
    const operationId = newIdempotencyKey();
    operations.current.set(source.key, operationId);
    updateSource(source.key, {
      chosenFileName: file.name,
      transferUpload: null,
      progress: null,
      status: "preparing",
      error: null,
    });

    const uploader = new ResumableUpload(file, {
      onProgress: (progress) => updateIfCurrent(source.key, operationId, { progress }),
      onStateChange: (upload) =>
        updateIfCurrent(source.key, operationId, { transferUpload: upload }),
    });
    uploaders.current.set(source.key, uploader);

    try {
      const completed = await uploader.start();
      const verified = await waitForVerification(completed.id, (upload) =>
        updateIfCurrent(source.key, operationId, { transferUpload: upload }),
      );
      if (verified.state === "failed") {
        throw new Error(verified.error?.message ?? "The file could not be verified.");
      }
      if (operations.current.get(source.key) !== operationId) {
        await api.deleteUpload(verified.id).catch(() => undefined);
        return;
      }
      updateSource(source.key, {
        upload: verified,
        transferUpload: null,
        progress: null,
        status: "ready",
        error: null,
      });
      if (source.upload && source.upload.id !== verified.id) {
        await api.deleteUpload(source.upload.id).catch(() => undefined);
      }
    } catch (caught) {
      if (operations.current.get(source.key) === operationId && !(caught instanceof UploadCancelled)) {
        updateSource(source.key, {
          transferUpload: null,
          progress: null,
          status: "failed",
          error: caught instanceof ApiError ? caught.message : (caught as Error).message,
        });
      }
    } finally {
      finishOperation(source.key, operationId);
    }
  };

  const prepareUrl = async (source: SourceRow) => {
    const url = source.url.trim();
    if (source.status === "preparing") return;
    if (!validVideoUrl(url)) {
      updateSource(source.key, { error: "Enter a complete public http:// or https:// video link." });
      return;
    }

    const operationId = newIdempotencyKey();
    operations.current.set(source.key, operationId);
    updateSource(source.key, {
      transferUpload: null,
      progress: null,
      status: "preparing",
      error: null,
    });

    try {
      const created = await api.createUrlUpload(url, newIdempotencyKey());
      if (operations.current.get(source.key) !== operationId) {
        await api.deleteUpload(created.id).catch(() => undefined);
        return;
      }
      remoteUploads.current.set(source.key, created.id);
      updateSource(source.key, { transferUpload: created });
      const verified = await waitForVerification(created.id, (upload) =>
        updateIfCurrent(source.key, operationId, { transferUpload: upload }),
      );
      if (verified.state === "failed") {
        throw new Error(verified.error?.message ?? "The video could not be downloaded.");
      }
      if (operations.current.get(source.key) !== operationId) {
        await api.deleteUpload(verified.id).catch(() => undefined);
        return;
      }
      setSources((current) =>
        current.map((row) =>
          row.key === source.key
            ? {
                ...row,
                preparedUrl: url,
                upload: verified,
                transferUpload: null,
                progress: null,
                sourceName:
                  !row.sourceNameTouched && verified.suggestedSourceName
                    ? verified.suggestedSourceName
                    : row.sourceName,
                status: "ready",
                error: null,
              }
            : row,
        ),
      );
      if (source.upload && source.upload.id !== verified.id) {
        await api.deleteUpload(source.upload.id).catch(() => undefined);
      }
    } catch (caught) {
      if (operations.current.get(source.key) === operationId) {
        updateSource(source.key, {
          transferUpload: null,
          progress: null,
          status: source.upload ? "ready" : "failed",
          error: caught instanceof ApiError ? caught.message : (caught as Error).message,
        });
      }
    } finally {
      finishOperation(source.key, operationId);
    }
  };

  const prepareAllLinks = () => {
    setError(null);
    sources
      .filter(
        (source) =>
          source.kind === "url" &&
          source.status !== "preparing" &&
          !sourceIsReady(source) &&
          validVideoUrl(source.url),
      )
      .forEach((source) => void prepareUrl(source));
  };

  const startJob = async () => {
    if (!sources.length || !sources.every(sourceIsReady)) {
      setError("Prepare every source before starting the analysis.");
      return;
    }
    setStarting(true);
    setError(null);
    const inputs: JobSourceInput[] = sources.map((source) => ({
      uploadId: source.upload!.id,
      sourceName: source.sourceName.trim() || null,
      contentPrompt: source.contentPrompt.trim() || null,
    }));
    try {
      const job = await api.createMultiSourceJob(
        inputs,
        targetClipCount,
        rankingEnabled,
        rankingEnabled && useGemini,
        newIdempotencyKey(),
      );
      navigate(`/jobs/${job.id}`);
    } catch (caught) {
      setError(jobCreationError(caught));
      setStarting(false);
    }
  };

  const readyCount = sources.filter(sourceIsReady).length;
  const allReady = sources.length > 0 && readyCount === sources.length;
  const pendingLinkCount = sources.filter(
    (source) =>
      source.kind === "url" &&
      source.status !== "preparing" &&
      !sourceIsReady(source) &&
      validVideoUrl(source.url),
  ).length;

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Rise>
        <GlowBorder active={!starting}>
          <div className="relative overflow-hidden rounded-[calc(var(--radius-panel)-1px)] bg-gradient-to-br from-navy-50 via-paper to-azure-50 p-6 sm:p-8">
            <div
              aria-hidden
              className="pointer-events-none absolute -right-20 -top-24 size-72 rounded-full bg-azure-300/25 blur-3xl"
            />
            <div className="relative">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-azure-100 bg-paper px-3 py-1 text-xs font-semibold text-azure-700 shadow-soft">
                <IconSparkle style={{ height: 13, width: 13 }} /> Multi-source scene detection
              </span>
              <h1 className="mt-4 max-w-xl text-2xl font-semibold leading-tight tracking-tight text-ink-900 sm:text-[28px]">
                Build one clip collection from several videos
              </h1>
              <p className="mt-2.5 max-w-2xl text-sm leading-relaxed text-ink-500">
                Add each source in the order you want it reviewed. Links and files prepare
                concurrently without changing that order, and every source keeps its own name and
                instruction. Clips stay between {MIN_CLIP_SECONDS} and {MAX_CLIP_SECONDS} seconds.
              </p>
            </div>
          </div>
        </GlowBorder>
      </Rise>

      {error && (
        <Alert tone="rose" role="alert">
          {error}
        </Alert>
      )}

      <Rise>
        <Panel className="space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-ink-900">Sources in review order</h2>
              <p className="mt-1 text-xs text-ink-400">
                {sources.length
                  ? `${readyCount} of ${sources.length} ready. This numbered order stays fixed while transfers finish.`
                  : "Add a link or video file to begin."}
              </p>
            </div>
            {pendingLinkCount > 1 && (
              <Button variant="outline" size="sm" onClick={prepareAllLinks} disabled={starting}>
                <IconDownload style={{ height: 14, width: 14 }} /> Prepare all links
              </Button>
            )}
          </div>

          {sources.map((source, index) => {
            const sourceNumber = index + 1;
            const isReady = sourceIsReady(source);
            const linkChanged = source.kind === "url" && Boolean(source.upload) && !isReady;
            const progress =
              source.kind === "file"
                ? source.progress?.percent ?? source.transferUpload?.progressPercent ?? 0
                : source.transferUpload?.progressPercent ?? 0;
            const indeterminate =
              source.kind === "url" &&
              source.status === "preparing" &&
              !source.transferUpload?.declaredSizeBytes;

            return (
              <div
                key={source.key}
                className="rounded-2xl border border-line bg-canvas p-4 sm:p-5"
                data-testid={`source-row-${sourceNumber}`}
              >
                <div className="mb-4 flex items-start gap-3">
                  <span className="grid size-10 flex-none place-items-center rounded-xl bg-navy-900 text-white">
                    {source.kind === "url" ? (
                      <IconLink style={{ height: 17, width: 17 }} />
                    ) : (
                      <IconFilm style={{ height: 17, width: 17 }} />
                    )}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-sm font-semibold text-ink-900">
                        Source {sourceNumber} · {source.kind === "url" ? "Video link" : "Video file"}
                      </h3>
                      <span
                        className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                          isReady
                            ? "bg-ok-50 text-ok-600"
                            : source.status === "failed" || linkChanged
                              ? "bg-warn-50 text-warn-600"
                              : "bg-paper text-ink-400"
                        }`}
                      >
                        {sourceStatus(source)}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-ink-400">
                      Its name and instruction stay attached to this position.
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={starting}
                    aria-label={`Remove source ${sourceNumber}`}
                    onClick={() => void removeSource(source)}
                  >
                    <IconX style={{ height: 14, width: 14 }} /> Remove
                  </Button>
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  {source.kind === "url" ? (
                    <Field
                      label={`Video link for source ${sourceNumber}`}
                      hint={linkChanged ? "The link changed. Prepare it again before analysis." : undefined}
                    >
                      <Input
                        type="url"
                        inputMode="url"
                        value={source.url}
                        disabled={source.status === "preparing" || starting}
                        placeholder="https://www.youtube.com/watch?v=…"
                        onChange={(event) =>
                          updateSource(source.key, { url: event.target.value, error: null })
                        }
                      />
                    </Field>
                  ) : (
                    <Field
                      label={`Video file for source ${sourceNumber}`}
                      hint={source.chosenFileName ?? "MP4, MOV, MKV, or WebM"}
                    >
                      <div
                        onDragOver={(event) => {
                          event.preventDefault();
                          if (source.status !== "preparing") setDraggingKey(source.key);
                        }}
                        onDragLeave={() => setDraggingKey(null)}
                        onDrop={(event) => {
                          event.preventDefault();
                          setDraggingKey(null);
                          const file = event.dataTransfer.files[0];
                          if (file) void prepareFile(source, file);
                        }}
                        className={`flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed px-3 py-2.5 text-sm font-semibold transition ${
                          draggingKey === source.key
                            ? "border-azure-500 bg-azure-50 text-azure-700"
                            : "border-line-strong bg-paper text-ink-600 hover:border-azure-300"
                        } ${source.status === "preparing" || starting ? "pointer-events-none opacity-50" : ""}`}
                      >
                        <input
                          type="file"
                          accept={ACCEPTED}
                          hidden
                          disabled={source.status === "preparing" || starting}
                          aria-label={`Video file for source ${sourceNumber}`}
                          onChange={(event) => {
                            const file = event.target.files?.[0];
                            if (file) void prepareFile(source, file);
                            event.target.value = "";
                          }}
                        />
                        <IconUpload style={{ height: 16, width: 16 }} />
                        {source.upload ? "Replace video file" : "Choose or drop video file"}
                      </div>
                    </Field>
                  )}

                  <Field
                    label={`Source name for source ${sourceNumber}`}
                    hint={
                      source.kind === "url" && !source.sourceNameTouched && source.sourceName
                        ? `${source.sourceName.length} / ${MAX_SOURCE_NAME_CHARS}. Filled from the video channel; you can edit it.`
                        : `${source.sourceName.length} / ${MAX_SOURCE_NAME_CHARS}. Editable before and after preparation.`
                    }
                  >
                    <Input
                      maxLength={MAX_SOURCE_NAME_CHARS}
                      value={source.sourceName}
                      disabled={starting}
                      placeholder="e.g. Global Times"
                      onChange={(event) =>
                        updateSource(source.key, {
                          sourceName: event.target.value,
                          sourceNameTouched: true,
                        })
                      }
                    />
                  </Field>
                </div>

                <Field
                  label={`Instruction for source ${sourceNumber} (optional)`}
                  hint="Used only if automatic ranking is enabled."
                  className="mt-4"
                >
                  <Textarea
                    rows={2}
                    maxLength={MAX_PROMPT_CHARS}
                    value={source.contentPrompt}
                    disabled={starting}
                    placeholder="e.g. prioritize clear product shots without people"
                    onChange={(event) =>
                      updateSource(source.key, { contentPrompt: event.target.value })
                    }
                  />
                </Field>

                {source.status === "preparing" && (
                  <div className="mt-4 rounded-xl border border-line bg-paper p-3">
                    <div className="mb-2 flex items-center gap-3">
                      <p className="min-w-0 flex-1 truncate text-xs font-medium text-ink-600">
                        {source.kind === "url" ? source.url : source.chosenFileName}
                      </p>
                      <span className="text-xs font-semibold tabular-nums text-azure-700">
                        {indeterminate ? "Working…" : `${Math.round(progress)}%`}
                      </span>
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => void cancelPreparation(source)}
                      >
                        Cancel
                      </Button>
                    </div>
                    <Progress value={progress} indeterminate={indeterminate} />
                  </div>
                )}

                {source.error && (
                  <Alert tone="rose" role="alert" className="mt-4">
                    {source.error}
                  </Alert>
                )}

                {isReady && source.upload && (
                  <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-ok-100 bg-ok-50 px-3 py-2 text-xs text-ok-600">
                    <span className="font-semibold">Ready</span>
                    <span className="min-w-0 truncate">{source.upload.fileName}</span>
                    <span>{formatBytes(source.upload.declaredSizeBytes)}</span>
                  </div>
                )}

                {source.kind === "url" && source.status !== "preparing" && !isReady && (
                  <Button
                    variant="primary"
                    size="sm"
                    className="mt-4"
                    disabled={!validVideoUrl(source.url) || starting}
                    onClick={() => void prepareUrl(source)}
                  >
                    <IconDownload style={{ height: 14, width: 14 }} />
                    {source.upload ? "Prepare updated link" : "Download and prepare video"}
                  </Button>
                )}
              </div>
            );
          })}

          {sources.length < MAX_SOURCES && !starting && (
            <div className="grid gap-2 border-t border-line pt-4 sm:grid-cols-2">
              <Button variant="outline" onClick={() => addSource("url")}>
                <IconPlus style={{ height: 15, width: 15 }} /> Add another video link
              </Button>
              <Button variant="outline" onClick={() => addSource("file")}>
                <IconPlus style={{ height: 15, width: 15 }} /> Add a video file
              </Button>
            </div>
          )}
          {sources.length === MAX_SOURCES && (
            <p className="text-center text-xs text-ink-400">
              Maximum of {MAX_SOURCES} sources reached.
            </p>
          )}
        </Panel>
      </Rise>

      {sources.length > 0 && (
        <Rise delay={0.08}>
          <Panel className="space-y-5">
            <Switch
              checked={rankingEnabled}
              disabled={starting}
              onChange={setRankingEnabled}
              label="Automatically rank and preselect clips"
              description="Turn this off for a blank manual review where you choose every clip yourself."
            />
            {rankingEnabled && (
              <div className="space-y-5 rounded-xl border border-line bg-canvas p-4">
                <Field
                  label="Target clip count"
                  hint="How many top-ranked clips should be preselected."
                >
                  <Input
                    type="number"
                    min={1}
                    max={100}
                    value={targetClipCount}
                    onChange={(event) =>
                      setTargetClipCount(
                        Math.max(1, Math.min(100, Number(event.target.value) || 1)),
                      )
                    }
                  />
                </Field>
                <Switch
                  checked={useGemini}
                  disabled={starting}
                  onChange={setUseGemini}
                  label="Use Gemini for ranking"
                  description="Off uses local measurements only. On sends representative frames according to Settings."
                />
              </div>
            )}
            <ShimmerButton
              onClick={() => void startJob()}
              disabled={starting || !allReady}
              className="w-full"
            >
              {starting
                ? "Starting…"
                : `Analyse ${sources.length} source${sources.length === 1 ? "" : "s"}`}
            </ShimmerButton>
            {!allReady && (
              <p className="text-center text-xs text-ink-400">
                Prepare every numbered source first. Names can be entered or edited at any time.
              </p>
            )}
          </Panel>
        </Rise>
      )}
    </div>
  );
}
