import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion } from "motion/react";

import { MAX_CLIP_SECONDS, MIN_CLIP_SECONDS } from "../lib/clip";
import { ApiError, api, newIdempotencyKey } from "../api/client";
import type { Upload } from "../api/types";
import { Button, ShimmerButton } from "../components/ui/Button";
import { IconCheck, IconFilm, IconSparkle, IconUpload } from "../components/ui/Icons";
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
import { formatBytes, formatEta, formatRate } from "../lib/format";
import {
  ResumableUpload,
  UploadCancelled,
  type UploadProgress,
  waitForVerification,
} from "../lib/uploader";

const MAX_PROMPT_CHARS = 2000;
const MAX_SOURCE_NAME_CHARS = 48;
const ACCEPTED = ".mp4,.mov,.mkv,.webm,video/*";
const CONTAINERS = ["MP4", "MOV", "MKV", "WebM"];

type Phase = "choose" | "uploading" | "verifying" | "configure" | "starting";

export function NewJobPage() {
  const navigate = useNavigate();
  const [phase, setPhase] = useState<Phase>("choose");
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const [targetClipCount, setTargetClipCount] = useState(20);
  const [contentPrompt, setContentPrompt] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [useGemini, setUseGemini] = useState(true);

  const uploaderRef = useRef<ResumableUpload | null>(null);

  const beginUpload = async (chosen: File) => {
    setFile(chosen);
    setError(null);
    setPhase("uploading");

    const uploader = new ResumableUpload(chosen, {
      onProgress: setProgress,
      onStateChange: setUpload,
    });
    uploaderRef.current = uploader;

    try {
      const completed = await uploader.start();
      setPhase("verifying");
      const verified = await waitForVerification(completed.id, setUpload);
      if (verified.state === "failed") {
        setError(verified.error?.message ?? "The file could not be verified.");
        setPhase("choose");
        return;
      }
      setUpload(verified);
      setPhase("configure");
    } catch (caught) {
      if (caught instanceof UploadCancelled) {
        setPhase("choose");
        return;
      }
      setError(caught instanceof ApiError ? caught.message : "The upload failed.");
      setPhase("choose");
    }
  };

  const startJob = async () => {
    if (!upload) return;
    setPhase("starting");
    setError(null);
    try {
      const job = await api.createJob(
        upload.id,
        targetClipCount,
        contentPrompt.trim() || null,
        sourceName.trim() || null,
        useGemini,
        newIdempotencyKey(),
      );
      navigate(`/jobs/${job.id}`);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The job could not be created.");
      setPhase("configure");
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Rise>
        <GlowBorder active={phase === "choose"}>
          <div className="relative overflow-hidden rounded-[calc(var(--radius-panel)-1px)] bg-gradient-to-br from-navy-50 via-white to-azure-50 p-6 sm:p-8">
            <div
              aria-hidden
              className="pointer-events-none absolute -right-20 -top-24 size-72 rounded-full bg-azure-300/25 blur-3xl"
            />
            <div className="relative">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-azure-100 bg-white px-3 py-1 text-xs font-semibold text-azure-700 shadow-soft">
                <IconSparkle style={{ height: 13, width: 13 }} />
                Automatic shot detection
              </span>
              <h1 className="mt-4 max-w-md text-2xl font-semibold leading-tight tracking-tight text-ink-900 sm:text-[28px]">
                Make short clips from one long video, automatically
              </h1>
              <p className="mt-2.5 max-w-lg text-sm leading-relaxed text-ink-500">
                Every clip stays inside a single continuous shot — never crossing a cut, fade, or
                dissolve — and lands between {MIN_CLIP_SECONDS} and {MAX_CLIP_SECONDS} seconds.
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

      {phase === "choose" && (
        <Rise delay={0.08}>
          <label
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              const dropped = event.dataTransfer.files?.[0];
              if (dropped) void beginUpload(dropped);
            }}
            className={`group flex cursor-pointer flex-col items-center justify-center rounded-panel border-2 border-dashed px-6 py-16 text-center transition-all duration-300 ${
              dragging
                ? "border-azure-500 bg-azure-50 shadow-[0_0_0_6px_rgb(0_163_250_/_0.10)]"
                : "border-line-strong bg-paper hover:border-azure-300 hover:bg-azure-50/40"
            }`}
          >
            <input
              type="file"
              accept={ACCEPTED}
              hidden
              onChange={(event) => {
                const chosen = event.target.files?.[0];
                if (chosen) void beginUpload(chosen);
              }}
            />
            <motion.span
              animate={dragging ? { y: -6, scale: 1.06 } : { y: 0, scale: 1 }}
              transition={{ type: "spring", stiffness: 320, damping: 22 }}
              className="mb-5 grid size-16 place-items-center rounded-2xl bg-gradient-to-br from-navy-900 to-azure-600 text-white shadow-navy"
            >
              <IconUpload style={{ height: 24, width: 24 }} />
            </motion.span>
            <p className="text-lg font-semibold text-ink-900">Drop one video here</p>
            <p className="mt-1 text-sm text-ink-500">or click to browse — one video per job</p>

            <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
              {CONTAINERS.map((format) => (
                <span
                  key={format}
                  className="rounded-lg border border-line bg-canvas px-2.5 py-1 text-xs font-medium text-ink-500"
                >
                  {format}
                </span>
              ))}
            </div>
          </label>
        </Rise>
      )}

      {(phase === "uploading" || phase === "verifying") && (
        <Rise>
          <Panel>
            <div className="mb-4 flex items-center gap-3">
              <span className="grid size-11 flex-none place-items-center rounded-xl bg-gradient-to-br from-navy-900 to-azure-600 text-white shadow-navy">
                <IconFilm style={{ height: 19, width: 19 }} />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-ink-900">{file?.name}</p>
                <p className="text-xs text-ink-400">
                  {phase === "verifying"
                    ? "Checking the file hash and probing the media…"
                    : `${formatBytes(progress?.uploadedBytes ?? 0)} of ${formatBytes(file?.size ?? 0)}`}
                </p>
              </div>
              {phase === "uploading" && (
                <span className="font-display text-lg font-semibold tabular-nums text-azure-700">
                  {Math.round(progress?.percent ?? 0)}%
                </span>
              )}
            </div>

            <Progress
              value={progress?.percent ?? 0}
              indeterminate={phase === "verifying"}
              className="mb-3"
            />

            {phase === "uploading" && (
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-ink-400">
                  {formatRate(progress?.bytesPerSecond ?? 0)} ·{" "}
                  {formatEta(progress?.secondsRemaining ?? null)}
                </p>
                <Button variant="danger" size="sm" onClick={() => uploaderRef.current?.cancel()}>
                  Cancel upload
                </Button>
              </div>
            )}

            <p className="mt-3 text-xs leading-relaxed text-ink-400">
              The transfer is resumable: if it is interrupted it continues from the last verified
              byte rather than starting over.
            </p>
          </Panel>
        </Rise>
      )}

      {(phase === "configure" || phase === "starting") && upload && (
        <Rise>
          <Panel className="space-y-6">
            <div className="flex items-center gap-3 rounded-xl border border-ok-100 bg-ok-50 px-4 py-3">
              <span className="grid size-9 flex-none place-items-center rounded-lg bg-ok-500 text-white">
                <IconCheck style={{ height: 16, width: 16 }} strokeWidth={3} />
              </span>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-ink-900">{upload.fileName}</p>
                <p className="text-xs text-ok-600">
                  {formatBytes(upload.declaredSizeBytes)} · checksum verified
                </p>
              </div>
            </div>

            <Field
              label="Target clip count"
              hint="Between 1 and 100. If fewer usable shots exist the job still succeeds and reports the shortfall."
            >
              <div className="flex items-center gap-4">
                <input
                  type="range"
                  min={1}
                  max={100}
                  value={targetClipCount}
                  onChange={(event) => setTargetClipCount(Number(event.target.value))}
                  className="flex-1"
                />
                <Input
                  type="number"
                  min={1}
                  max={100}
                  value={targetClipCount}
                  onChange={(event) =>
                    setTargetClipCount(Number.parseInt(event.target.value, 10) || 1)
                  }
                  className="w-20 text-center tabular-nums"
                />
              </div>
            </Field>

            <Field
              label="Source name (optional)"
              hint={`${sourceName.length} / ${MAX_SOURCE_NAME_CHARS} characters. Shown in uppercase at the top left of review previews and exported clips. Leave blank for no label.`}
            >
              <Input
                type="text"
                maxLength={MAX_SOURCE_NAME_CHARS}
                value={sourceName}
                placeholder="e.g. Driver Sphere"
                onChange={(event) => setSourceName(event.target.value)}
              />
            </Field>

            <Field
              label="Product (optional)"
              hint={`${contentPrompt.length} / ${MAX_PROMPT_CHARS} characters. Name one product. Shots that clearly show it, with nobody in frame, are selected first — the rest stay available to pick by hand.`}
            >
              <Textarea
                rows={3}
                maxLength={MAX_PROMPT_CHARS}
                value={contentPrompt}
                placeholder="e.g. the matte black espresso machine with the brass handle"
                onChange={(event) => setContentPrompt(event.target.value)}
              />
            </Field>

            <div className="rounded-xl border border-line bg-canvas p-4">
              <Switch
                checked={useGemini}
                onChange={setUseGemini}
                label="Use Gemini to rank clips"
                description="Representative still frames — and rarely a short low-resolution excerpt — are sent. Your full source video is never sent."
              />
            </div>

            <ShimmerButton
              onClick={() => void startJob()}
              disabled={phase === "starting" || targetClipCount < 1 || targetClipCount > 100}
              className="w-full"
            >
              <IconSparkle style={{ height: 16, width: 16 }} />
              {phase === "starting" ? "Starting…" : "Start analysis"}
            </ShimmerButton>
          </Panel>
        </Rise>
      )}
    </div>
  );
}
