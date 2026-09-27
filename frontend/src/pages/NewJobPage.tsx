import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion } from "motion/react";

import { MAX_CLIP_SECONDS, MIN_CLIP_SECONDS } from "../lib/clip";
import { ApiError, api, newIdempotencyKey } from "../api/client";
import type { JobSourceInput, Upload } from "../api/types";
import { Button, ShimmerButton } from "../components/ui/Button";
import {
  IconDownload,
  IconFilm,
  IconLink,
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
type SourceMode = "file" | "url";

interface ReadySource {
  key: string;
  upload: Upload;
  sourceName: string;
  contentPrompt: string;
}

interface Transfer {
  key: string;
  label: string;
  kind: SourceMode;
  upload: Upload | null;
  progress: UploadProgress | null;
}

export function NewJobPage() {
  const navigate = useNavigate();
  const [sourceMode, setSourceMode] = useState<SourceMode>("file");
  const [sources, setSources] = useState<ReadySource[]>([]);
  const [transfers, setTransfers] = useState<Transfer[]>([]);
  const [videoUrls, setVideoUrls] = useState("");
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [rankingEnabled, setRankingEnabled] = useState(false);
  const [useGemini, setUseGemini] = useState(false);
  const [targetClipCount, setTargetClipCount] = useState(20);

  const uploaders = useRef(new Map<string, ResumableUpload>());
  const remoteUploads = useRef(new Map<string, string>());
  const cancelled = useRef(new Set<string>());

  const updateTransfer = (key: string, change: Partial<Transfer>) =>
    setTransfers((current) =>
      current.map((transfer) => (transfer.key === key ? { ...transfer, ...change } : transfer)),
    );

  const finishTransfer = (key: string) => {
    uploaders.current.delete(key);
    remoteUploads.current.delete(key);
    setTransfers((current) => current.filter((transfer) => transfer.key !== key));
  };

  const addReadySource = (key: string, upload: Upload) => {
    if (cancelled.current.has(key)) return;
    setSources((current) => [
      ...current,
      { key, upload, sourceName: "", contentPrompt: "" },
    ]);
  };

  const addFile = async (file: File) => {
    const key = newIdempotencyKey();
    setTransfers((current) => [
      ...current,
      { key, label: file.name, kind: "file", upload: null, progress: null },
    ]);
    const uploader = new ResumableUpload(file, {
      onProgress: (progress) => updateTransfer(key, { progress }),
      onStateChange: (upload) => updateTransfer(key, { upload }),
    });
    uploaders.current.set(key, uploader);

    try {
      const completed = await uploader.start();
      const verified = await waitForVerification(completed.id, (upload) =>
        updateTransfer(key, { upload }),
      );
      if (verified.state === "failed") {
        throw new Error(verified.error?.message ?? "The file could not be verified.");
      }
      addReadySource(key, verified);
    } catch (caught) {
      if (!(caught instanceof UploadCancelled) && !cancelled.current.has(key)) {
        setError(caught instanceof ApiError ? caught.message : (caught as Error).message);
      }
    } finally {
      finishTransfer(key);
    }
  };

  const addFiles = (files: FileList | File[]) => {
    setError(null);
    const room = Math.max(0, MAX_SOURCES - sources.length - transfers.length);
    const chosen = Array.from(files).slice(0, room);
    if (chosen.length < files.length) setError(`A job can contain up to ${MAX_SOURCES} videos.`);
    chosen.forEach((file) => void addFile(file));
  };

  const addUrl = async (url: string) => {
    const key = newIdempotencyKey();
    setTransfers((current) => [
      ...current,
      { key, label: url, kind: "url", upload: null, progress: null },
    ]);
    try {
      const created = await api.createUrlUpload(url, newIdempotencyKey());
      remoteUploads.current.set(key, created.id);
      updateTransfer(key, { upload: created });
      if (cancelled.current.has(key)) {
        await api.deleteUpload(created.id).catch(() => undefined);
        return;
      }
      const verified = await waitForVerification(created.id, (upload) =>
        updateTransfer(key, { upload }),
      );
      if (verified.state === "failed") {
        throw new Error(verified.error?.message ?? "The video could not be downloaded.");
      }
      addReadySource(key, verified);
    } catch (caught) {
      if (!cancelled.current.has(key)) {
        setError(caught instanceof ApiError ? caught.message : (caught as Error).message);
      }
    } finally {
      finishTransfer(key);
    }
  };

  const addLinks = () => {
    const links = videoUrls
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!links.length) return;
    setError(null);
    const room = Math.max(0, MAX_SOURCES - sources.length - transfers.length);
    const chosen = links.slice(0, room);
    if (chosen.length < links.length) setError(`A job can contain up to ${MAX_SOURCES} videos.`);
    setVideoUrls("");
    chosen.forEach((url) => void addUrl(url));
  };

  const cancelTransfer = async (transfer: Transfer) => {
    cancelled.current.add(transfer.key);
    uploaders.current.get(transfer.key)?.cancel();
    const uploadId = transfer.upload?.id ?? remoteUploads.current.get(transfer.key);
    finishTransfer(transfer.key);
    if (uploadId) await api.deleteUpload(uploadId).catch(() => undefined);
  };

  const removeSource = async (source: ReadySource) => {
    setSources((current) => current.filter((item) => item.key !== source.key));
    await api.deleteUpload(source.upload.id).catch(() => undefined);
  };

  const updateSource = (key: string, change: Partial<ReadySource>) =>
    setSources((current) =>
      current.map((source) => (source.key === key ? { ...source, ...change } : source)),
    );

  const startJob = async () => {
    if (!sources.length || transfers.length) return;
    setStarting(true);
    setError(null);
    const inputs: JobSourceInput[] = sources.map((source) => ({
      uploadId: source.upload.id,
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
      setError(caught instanceof ApiError ? caught.message : "The job could not be created.");
      setStarting(false);
    }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Rise>
        <GlowBorder active={!starting}>
          <div className="relative overflow-hidden rounded-[calc(var(--radius-panel)-1px)] bg-gradient-to-br from-navy-50 via-paper to-azure-50 p-6 sm:p-8">
            <div aria-hidden className="pointer-events-none absolute -right-20 -top-24 size-72 rounded-full bg-azure-300/25 blur-3xl" />
            <div className="relative">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-azure-100 bg-paper px-3 py-1 text-xs font-semibold text-azure-700 shadow-soft">
                <IconSparkle style={{ height: 13, width: 13 }} /> Multi-source scene detection
              </span>
              <h1 className="mt-4 max-w-xl text-2xl font-semibold leading-tight tracking-tight text-ink-900 sm:text-[28px]">
                Build one clip collection from several videos
              </h1>
              <p className="mt-2.5 max-w-2xl text-sm leading-relaxed text-ink-500">
                Mix uploaded files and public links. Sources are analysed concurrently, while every
                {" "}source keeps its own label and instruction. Clips stay between {MIN_CLIP_SECONDS}
                {" "}and {MAX_CLIP_SECONDS} seconds without crossing a transition.
              </p>
            </div>
          </div>
        </GlowBorder>
      </Rise>

      {error && <Alert tone="rose" role="alert">{error}</Alert>}

      {sources.length > 0 && (
        <Rise>
          <Panel className="space-y-4">
            <div>
              <h2 className="text-base font-semibold text-ink-900">
                {sources.length} source{sources.length === 1 ? "" : "s"} ready
              </h2>
              <p className="mt-1 text-xs text-ink-400">
                Labels and instructions apply only to the source where you enter them.
              </p>
            </div>
            {sources.map((source, index) => (
              <div key={source.key} className="rounded-xl border border-line bg-canvas p-4">
                <div className="mb-4 flex items-start gap-3">
                  <span className="grid size-10 flex-none place-items-center rounded-xl bg-navy-900 text-white">
                    <IconFilm style={{ height: 17, width: 17 }} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold text-ink-900">
                      {index + 1}. {source.upload.fileName}
                    </p>
                    <p className="text-xs text-ink-400">
                      {formatBytes(source.upload.declaredSizeBytes)} · {source.upload.sourceKind === "url" ? "link" : "file"}
                    </p>
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => void removeSource(source)}>
                    <IconX style={{ height: 14, width: 14 }} /> Remove
                  </Button>
                </div>
                <div className="grid gap-4 md:grid-cols-2">
                  <Field label={`Source name for video ${index + 1}`} hint={`${source.sourceName.length} / ${MAX_SOURCE_NAME_CHARS}. Appears at the top left of this source's exported clips.`}>
                    <Input maxLength={MAX_SOURCE_NAME_CHARS} value={source.sourceName} placeholder="e.g. Driver Sphere" onChange={(event) => updateSource(source.key, { sourceName: event.target.value })} />
                  </Field>
                  <Field label={`Instruction for video ${index + 1} (optional)`} hint="Used only if automatic ranking is enabled.">
                    <Textarea rows={2} maxLength={MAX_PROMPT_CHARS} value={source.contentPrompt} placeholder="e.g. prioritize clear product shots without people" onChange={(event) => updateSource(source.key, { contentPrompt: event.target.value })} />
                  </Field>
                </div>
              </div>
            ))}
          </Panel>
        </Rise>
      )}

      {transfers.length > 0 && (
        <Rise>
          <Panel className="space-y-3">
            <h2 className="text-base font-semibold text-ink-900">Preparing sources</h2>
            {transfers.map((transfer) => {
              const progress = transfer.kind === "file" ? transfer.progress?.percent ?? 0 : transfer.upload?.progressPercent ?? 0;
              const indeterminate = transfer.kind === "url" && !transfer.upload?.declaredSizeBytes;
              return (
                <div key={transfer.key} className="rounded-xl border border-line bg-canvas p-3">
                  <div className="mb-2 flex items-center gap-3">
                    <p className="min-w-0 flex-1 truncate text-sm font-medium text-ink-800">{transfer.label}</p>
                    <span className="text-sm font-semibold tabular-nums text-azure-700">{Math.round(progress)}%</span>
                    <Button variant="danger" size="sm" onClick={() => void cancelTransfer(transfer)}>Cancel</Button>
                  </div>
                  <Progress value={progress} indeterminate={indeterminate} />
                </div>
              );
            })}
          </Panel>
        </Rise>
      )}

      {sources.length + transfers.length < MAX_SOURCES && !starting && (
        <Rise delay={0.05}>
          <Panel className="p-2 sm:p-2">
            <div className="mb-2 grid grid-cols-2 gap-1 rounded-xl bg-canvas p-1" role="tablist">
              {([["file", "Add video files", IconUpload], ["url", "Paste video links", IconLink]] as const).map(([mode, label, Icon]) => (
                <button key={mode} type="button" role="tab" aria-selected={sourceMode === mode} onClick={() => setSourceMode(mode)} className={`inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2.5 text-sm font-semibold transition ${sourceMode === mode ? "bg-paper text-azure-700 shadow-soft" : "text-ink-500 hover:text-ink-900"}`}>
                  <Icon style={{ height: 16, width: 16 }} /> {label}
                </button>
              ))}
            </div>
            {sourceMode === "file" ? (
              <label
                onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={(event) => { event.preventDefault(); setDragging(false); addFiles(event.dataTransfer.files); }}
                className={`flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center transition ${dragging ? "border-azure-500 bg-azure-50" : "border-line-strong bg-paper hover:border-azure-300"}`}
              >
                <input type="file" accept={ACCEPTED} multiple hidden onChange={(event) => event.target.files && addFiles(event.target.files)} />
                <motion.span animate={dragging ? { y: -4, scale: 1.05 } : { y: 0, scale: 1 }} className="mb-4 grid size-14 place-items-center rounded-2xl bg-gradient-to-br from-navy-900 to-azure-600 text-white shadow-navy">
                  <IconUpload style={{ height: 22, width: 22 }} />
                </motion.span>
                <p className="font-semibold text-ink-900">Drop one or several videos here</p>
                <p className="mt-1 text-sm text-ink-500">or click to choose MP4, MOV, MKV, or WebM files</p>
              </label>
            ) : (
              <form className="rounded-xl border border-line bg-paper p-5" onSubmit={(event) => { event.preventDefault(); addLinks(); }}>
                <Field label="Video links" hint="One public video URL per line. They download concurrently.">
                  <Textarea rows={4} required value={videoUrls} aria-label="Video links" placeholder={"https://www.youtube.com/watch?v=…\nhttps://vimeo.com/…"} onChange={(event) => setVideoUrls(event.target.value)} />
                </Field>
                <ShimmerButton type="submit" disabled={!videoUrls.trim()} className="mt-3 w-full">
                  <IconDownload style={{ height: 16, width: 16 }} /> Add links
                </ShimmerButton>
              </form>
            )}
          </Panel>
        </Rise>
      )}

      {sources.length > 0 && (
        <Rise delay={0.08}>
          <Panel className="space-y-5">
            <Switch checked={rankingEnabled} disabled={starting} onChange={setRankingEnabled} label="Automatically rank and preselect clips" description="Turn this off for a blank manual review where you choose every clip yourself." />
            {rankingEnabled && (
              <div className="space-y-5 rounded-xl border border-line bg-canvas p-4">
                <Field label="Target clip count" hint="How many top-ranked clips should be preselected.">
                  <Input type="number" min={1} max={100} value={targetClipCount} onChange={(event) => setTargetClipCount(Math.max(1, Math.min(100, Number(event.target.value) || 1)))} />
                </Field>
                <Switch checked={useGemini} disabled={starting} onChange={setUseGemini} label="Use Gemini for ranking" description="Off uses local measurements only. On sends representative frames according to Settings." />
              </div>
            )}
            <ShimmerButton onClick={() => void startJob()} disabled={starting || transfers.length > 0} className="w-full">
              {starting ? "Starting…" : `Analyse ${sources.length} source${sources.length === 1 ? "" : "s"}`}
            </ShimmerButton>
            {transfers.length > 0 && <p className="text-center text-xs text-ink-400">Wait for every source to finish preparing before starting.</p>}
          </Panel>
        </Rise>
      )}
    </div>
  );
}
