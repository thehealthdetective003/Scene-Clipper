/**
 * Resumable chunked uploader (spec 5.3).
 *
 * Chunks are sequential and idempotent by offset + checksum, so an interrupted
 * transfer resumes from the server's verified offset without retransmitting
 * bytes that are already stored. A rejected chunk is retried against the offset
 * the server reports, which also covers the case where a chunk landed but its
 * response was lost.
 */

import { ApiError, api, newIdempotencyKey } from "../api/client";
import type { Upload } from "../api/types";

export interface UploadProgress {
  uploadedBytes: number;
  totalBytes: number;
  percent: number;
  /** Bytes per second over a short trailing window. */
  bytesPerSecond: number;
  secondsRemaining: number | null;
}

export interface UploaderCallbacks {
  onProgress?: (progress: UploadProgress) => void;
  onStateChange?: (upload: Upload) => void;
}

export class UploadCancelled extends Error {
  constructor() {
    super("The upload was cancelled.");
    this.name = "UploadCancelled";
  }
}

async function sha256Base64(buffer: ArrayBuffer): Promise<string> {
  // Web Crypto requires a secure context; the app is HTTPS or loopback only.
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  let binary = "";
  const bytes = new Uint8Array(digest);
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** How long a rejected-but-retryable chunk waits, with jitter. */
function backoffMs(attempt: number): number {
  return Math.min(8000, 400 * 2 ** attempt) * (0.5 + Math.random() * 0.5);
}

export class ResumableUpload {
  private cancelled = false;
  private controller: AbortController | null = null;

  constructor(
    private readonly file: File,
    private readonly callbacks: UploaderCallbacks = {},
  ) {}

  cancel(): void {
    this.cancelled = true;
    this.controller?.abort();
  }

  async start(existingUploadId?: string): Promise<Upload> {
    let upload: Upload;
    if (existingUploadId) {
      upload = await api.getUpload(existingUploadId);
    } else {
      upload = await api.createUpload(
        this.file.name,
        this.file.size,
        this.file.type || "application/octet-stream",
        newIdempotencyKey(),
      );
    }
    this.callbacks.onStateChange?.(upload);

    const chunkSize = upload.chunkSizeBytes;
    let offset = upload.verifiedOffsetBytes;
    const startedAt = performance.now();
    const startOffset = offset;

    while (offset < this.file.size) {
      if (this.cancelled) throw new UploadCancelled();

      const slice = this.file.slice(offset, Math.min(offset + chunkSize, this.file.size));
      const buffer = await slice.arrayBuffer();
      const checksum = await sha256Base64(buffer);

      let attempt = 0;
      for (;;) {
        if (this.cancelled) throw new UploadCancelled();
        this.controller = new AbortController();
        try {
          offset = await api.putChunk(upload.id, offset, buffer, checksum, this.controller.signal);
          break;
        } catch (error) {
          if (this.cancelled) throw new UploadCancelled();
          if (!(error instanceof ApiError)) throw error;

          // The server knows the truth about how far the transfer got.
          const expected = error.details?.expectedOffset;
          if (error.code === "upload_offset_mismatch" && typeof expected === "number") {
            offset = expected;
            break;
          }
          // A mismatched retransmission means the local slice is wrong for this
          // offset; realign from the server and rebuild the slice.
          if (error.code === "upload_chunk_mismatch") {
            offset = await api.uploadOffset(upload.id);
            break;
          }
          if (!error.retryable || attempt >= 4) throw error;
          await new Promise((resolve) => setTimeout(resolve, backoffMs(attempt)));
          attempt += 1;
        }
      }

      const elapsedSeconds = (performance.now() - startedAt) / 1000;
      const movedBytes = offset - startOffset;
      const rate = elapsedSeconds > 0 ? movedBytes / elapsedSeconds : 0;
      this.callbacks.onProgress?.({
        uploadedBytes: offset,
        totalBytes: this.file.size,
        percent: this.file.size ? (offset / this.file.size) * 100 : 0,
        bytesPerSecond: rate,
        secondsRemaining: rate > 0 ? (this.file.size - offset) / rate : null,
      });
    }

    const completed = await api.completeUpload(upload.id, newIdempotencyKey());
    this.callbacks.onStateChange?.(completed);
    return completed;
  }
}

/** Poll until server-side verification finishes (spec 5.3). */
export async function waitForVerification(
  uploadId: string,
  onUpdate?: (upload: Upload) => void,
  intervalMs = 1500,
): Promise<Upload> {
  for (;;) {
    const upload = await api.getUpload(uploadId);
    onUpdate?.(upload);
    if (upload.state === "ready" || upload.state === "failed") return upload;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}
