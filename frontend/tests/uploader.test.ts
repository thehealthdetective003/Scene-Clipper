/**
 * Uploader behaviour (spec 5.3, 12.3): resume from the verified offset, accept
 * an identical retransmission, and realign when the server reports a different
 * offset.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, setCsrfToken } from "../src/api/client";
import { ResumableUpload } from "../src/lib/uploader";
import type { Upload } from "../src/api/types";

function makeUpload(overrides: Partial<Upload> = {}): Upload {
  return {
    id: "upload-1",
    fileName: "clip.mp4",
    declaredSizeBytes: 300,
    verifiedOffsetBytes: 0,
    chunkSizeBytes: 100,
    state: "created",
    sha256: null,
    progressPercent: 0,
  sourceKind: "file",
  suggestedSourceName: null,
    error: null,
    createdAt: "2026-09-14T00:00:00Z",
    updatedAt: "2026-09-14T00:00:00Z",
    ...overrides,
  };
}

function makeFile(size: number): File {
  return new File([new Uint8Array(size).fill(65)], "clip.mp4", { type: "video/mp4" });
}

function apiError(code: string, details: Record<string, unknown> = {}, status = 409) {
  return new ApiError(status, {
    code,
    message: code,
    retryable: false,
    requestId: "req",
    details,
  });
}

describe("ResumableUpload", () => {
  beforeEach(() => {
    setCsrfToken("test-csrf");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("uploads sequential chunks and completes", async () => {
    const offsets: number[] = [];
    vi.spyOn(api, "createUpload").mockResolvedValue(makeUpload());
    vi.spyOn(api, "putChunk").mockImplementation(async (_id, offset, chunk) => {
      offsets.push(offset);
      return offset + chunk.byteLength;
    });
    const complete = vi
      .spyOn(api, "completeUpload")
      .mockResolvedValue(makeUpload({ state: "verifying", verifiedOffsetBytes: 300 }));

    const result = await new ResumableUpload(makeFile(300)).start();

    expect(offsets).toEqual([0, 100, 200]);
    expect(complete).toHaveBeenCalledOnce();
    expect(result.state).toBe("verifying");
  });

  it("resumes from the server's verified offset without resending bytes", async () => {
    const offsets: number[] = [];
    vi.spyOn(api, "getUpload").mockResolvedValue(
      makeUpload({ verifiedOffsetBytes: 200, state: "uploading" }),
    );
    vi.spyOn(api, "putChunk").mockImplementation(async (_id, offset, chunk) => {
      offsets.push(offset);
      return offset + chunk.byteLength;
    });
    vi.spyOn(api, "completeUpload").mockResolvedValue(makeUpload({ state: "verifying" }));

    await new ResumableUpload(makeFile(300)).start("upload-1");

    // Only the final chunk is transferred.
    expect(offsets).toEqual([200]);
  });

  it("realigns when the server reports a different expected offset", async () => {
    const offsets: number[] = [];
    vi.spyOn(api, "createUpload").mockResolvedValue(makeUpload());
    vi.spyOn(api, "putChunk").mockImplementation(async (_id, offset, chunk) => {
      offsets.push(offset);
      if (offset === 0 && offsets.filter((o) => o === 0).length === 1) {
        throw apiError("upload_offset_mismatch", { expectedOffset: 100 });
      }
      return offset + chunk.byteLength;
    });
    vi.spyOn(api, "completeUpload").mockResolvedValue(makeUpload({ state: "verifying" }));

    await new ResumableUpload(makeFile(300)).start();

    // After the mismatch the client continues from the server's offset.
    expect(offsets[0]).toBe(0);
    expect(offsets).toContain(100);
    expect(offsets).toContain(200);
  });

  it("re-reads the offset after a chunk mismatch", async () => {
    vi.spyOn(api, "createUpload").mockResolvedValue(makeUpload());
    const head = vi.spyOn(api, "uploadOffset").mockResolvedValue(100);
    let thrown = false;
    vi.spyOn(api, "putChunk").mockImplementation(async (_id, offset, chunk) => {
      if (!thrown) {
        thrown = true;
        throw apiError("upload_chunk_mismatch");
      }
      return offset + chunk.byteLength;
    });
    vi.spyOn(api, "completeUpload").mockResolvedValue(makeUpload({ state: "verifying" }));

    await new ResumableUpload(makeFile(300)).start();
    expect(head).toHaveBeenCalled();
  });

  it("reports progress as it goes", async () => {
    vi.spyOn(api, "createUpload").mockResolvedValue(makeUpload());
    vi.spyOn(api, "putChunk").mockImplementation(
      async (_id, offset, chunk) => offset + chunk.byteLength,
    );
    vi.spyOn(api, "completeUpload").mockResolvedValue(makeUpload({ state: "verifying" }));

    const seen: number[] = [];
    await new ResumableUpload(makeFile(300), {
      onProgress: (progress) => seen.push(progress.uploadedBytes),
    }).start();

    expect(seen).toEqual([100, 200, 300]);
  });

  it("stops when cancelled", async () => {
    vi.spyOn(api, "createUpload").mockResolvedValue(makeUpload());
    const uploader = new ResumableUpload(makeFile(300));
    vi.spyOn(api, "putChunk").mockImplementation(async (_id, offset, chunk) => {
      uploader.cancel();
      return offset + chunk.byteLength;
    });

    await expect(uploader.start()).rejects.toThrow(/cancelled/i);
  });
});
