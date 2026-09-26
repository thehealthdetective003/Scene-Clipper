/**
 * Typed API client.
 *
 * The CSRF token lives in a module-level variable and is never written to
 * localStorage, sessionStorage, IndexedDB, a URL, or any log (spec 5.2, 8.2).
 * The Gemini key is only ever passed as a request body field and is never
 * retained by this module.
 */

import type {
  ApiErrorBody,
  CandidatesResponse,
  ExportFilesResponse,
  ExportRecord,
  GeminiKeyTestResult,
  GeminiSettings,
  Job,
  JobList,
  Resolution,
  ReviewClipInput,
  ReviewResponse,
  SessionInfo,
  SourceLabelSettings,
  SourceLabelStyle,
  Upload,
} from "./types";

const BASE = "/api/v1";

/** In-memory only. Cleared on logout and on any 401. */
let csrfToken: string | null = null;

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;
  readonly requestId: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody["error"]) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.retryable = body.retryable;
    this.requestId = body.requestId;
    this.details = body.details ?? {};
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Raw bytes for upload chunks. */
  raw?: BodyInit;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  idempotencyKey?: string;
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { ...options.headers };

  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (UNSAFE.has(method) && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  if (options.idempotencyKey) {
    headers["Idempotency-Key"] = options.idempotencyKey;
  }

  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    // Same-origin only; the session cookie is HttpOnly and travels here.
    credentials: "same-origin",
    body: options.raw ?? (options.body !== undefined ? JSON.stringify(options.body) : undefined),
    signal: options.signal,
  });

  if (response.status === 401) {
    csrfToken = null;
  }

  if (!response.ok) {
    let body: ApiErrorBody["error"];
    try {
      body = ((await response.json()) as ApiErrorBody).error;
    } catch {
      body = {
        code: "request_failed",
        message: `Request failed with status ${response.status}.`,
        retryable: response.status >= 500,
        requestId: response.headers.get("X-Request-Id") ?? "unknown",
        details: {},
      };
    }
    throw new ApiError(response.status, body);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

// --- Auth and session ------------------------------------------------------

export const api = {
  async session(): Promise<SessionInfo> {
    const info = await request<SessionInfo>("/session");
    csrfToken = info.csrfToken;
    return info;
  },

  async login(username: string, password: string): Promise<void> {
    await request<void>("/auth/login", { method: "POST", body: { username, password } });
    await api.session();
  },

  async logout(): Promise<void> {
    await request<void>("/auth/logout", { method: "POST" });
    csrfToken = null;
  },

  // --- Settings ------------------------------------------------------------

  sourceLabelSettings(): Promise<SourceLabelSettings> {
    return request<SourceLabelSettings>("/settings/source-label");
  },

  saveSourceLabelSettings(style: SourceLabelStyle): Promise<SourceLabelSettings> {
    return request<SourceLabelSettings>("/settings/source-label", {
      method: "PUT",
      body: style,
    });
  },

  geminiSettings(): Promise<GeminiSettings> {
    return request<GeminiSettings>("/settings/gemini");
  },

  testGeminiKey(apiKey: string, model?: string): Promise<GeminiKeyTestResult> {
    // The key is sent once, in the body, and never stored client-side.
    return request<GeminiKeyTestResult>("/settings/gemini/test", {
      method: "POST",
      body: { apiKey, model },
    });
  },

  /** Append a key to the failover pool. Refused with 409 once it is full. */
  addGeminiKey(
    apiKey: string,
    model: string | undefined,
    requestCap: number,
    label?: string,
  ): Promise<GeminiSettings> {
    return request<GeminiSettings>("/settings/gemini", {
      method: "PUT",
      body: { apiKey, model, requestCap, label },
    });
  },

  /** Change model or cap without touching the stored keys. */
  saveGeminiPreferences(model: string | undefined, requestCap: number): Promise<GeminiSettings> {
    return request<GeminiSettings>("/settings/gemini", {
      method: "PATCH",
      body: { model, requestCap },
    });
  },

  renameGeminiKey(keyId: string, label: string): Promise<GeminiSettings> {
    return request<GeminiSettings>(`/settings/gemini/keys/${encodeURIComponent(keyId)}`, {
      method: "PATCH",
      body: { label },
    });
  },

  setGeminiKeyEnabled(keyId: string, enabled: boolean): Promise<GeminiSettings> {
    return request<GeminiSettings>(`/settings/gemini/keys/${encodeURIComponent(keyId)}`, {
      method: "PATCH",
      body: { enabled },
    });
  },

  /** Re-check one stored key; costs a single provider request. */
  testStoredGeminiKey(keyId: string): Promise<GeminiKeyTestResult> {
    return request<GeminiKeyTestResult>(
      `/settings/gemini/keys/${encodeURIComponent(keyId)}/test`,
      { method: "POST" },
    );
  },

  deleteGeminiKey(keyId: string): Promise<GeminiSettings> {
    return request<GeminiSettings>(`/settings/gemini/keys/${encodeURIComponent(keyId)}`, {
      method: "DELETE",
    });
  },

  deleteAllGeminiKeys(): Promise<void> {
    return request<void>("/settings/gemini", { method: "DELETE" });
  },

  // --- Uploads -------------------------------------------------------------

  createUpload(
    fileName: string,
    sizeBytes: number,
    mimeType: string,
    idempotencyKey: string,
  ): Promise<Upload> {
    return request<Upload>("/uploads", {
      method: "POST",
      body: { fileName, sizeBytes, mimeType },
      idempotencyKey,
    });
  },

  getUpload(uploadId: string): Promise<Upload> {
    return request<Upload>(`/uploads/${uploadId}`);
  },

  async uploadOffset(uploadId: string): Promise<number> {
    const response = await fetch(`${BASE}/uploads/${uploadId}`, {
      method: "HEAD",
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new ApiError(response.status, {
        code: "upload_head_failed",
        message: "Could not read the upload offset.",
        retryable: true,
        requestId: response.headers.get("X-Request-Id") ?? "unknown",
        details: {},
      });
    }
    return Number(response.headers.get("Upload-Offset") ?? "0");
  },

  async putChunk(
    uploadId: string,
    offset: number,
    chunk: ArrayBuffer,
    checksumBase64: string,
    signal?: AbortSignal,
  ): Promise<number> {
    const headers: Record<string, string> = {
      "Upload-Offset": String(offset),
      "Upload-Checksum": `sha256 ${checksumBase64}`,
      "Content-Type": "application/octet-stream",
    };
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;

    const response = await fetch(`${BASE}/uploads/${uploadId}/chunks`, {
      method: "PUT",
      headers,
      credentials: "same-origin",
      body: chunk,
      signal,
    });

    if (!response.ok) {
      let body: ApiErrorBody["error"];
      try {
        body = ((await response.json()) as ApiErrorBody).error;
      } catch {
        body = {
          code: "chunk_failed",
          message: `Chunk upload failed (${response.status}).`,
          retryable: response.status >= 500,
          requestId: "unknown",
          details: {},
        };
      }
      throw new ApiError(response.status, body);
    }
    return Number(response.headers.get("Upload-Offset") ?? String(offset + chunk.byteLength));
  },

  completeUpload(uploadId: string, idempotencyKey: string): Promise<Upload> {
    return request<Upload>(`/uploads/${uploadId}/complete`, {
      method: "POST",
      idempotencyKey,
    });
  },

  deleteUpload(uploadId: string): Promise<void> {
    return request<void>(`/uploads/${uploadId}`, { method: "DELETE" });
  },

  // --- Jobs ----------------------------------------------------------------

  listJobs(cursor?: string, limit = 25): Promise<JobList> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    return request<JobList>(`/jobs?${params.toString()}`);
  },

  createJob(
    uploadId: string,
    targetClipCount: number,
    contentPrompt: string | null,
    sourceName: string | null,
    useGemini: boolean,
    idempotencyKey: string,
  ): Promise<Job> {
    return request<Job>("/jobs", {
      method: "POST",
      body: { uploadId, targetClipCount, contentPrompt, sourceName, useGemini },
      idempotencyKey,
    });
  },

  getJob(jobId: string): Promise<Job> {
    return request<Job>(`/jobs/${jobId}`);
  },

  cancelJob(jobId: string): Promise<Job> {
    return request<Job>(`/jobs/${jobId}/cancel`, { method: "POST" });
  },

  retryJob(jobId: string, idempotencyKey: string): Promise<Job> {
    return request<Job>(`/jobs/${jobId}/retry`, { method: "POST", idempotencyKey });
  },

  deleteJob(jobId: string): Promise<void> {
    return request<void>(`/jobs/${jobId}`, { method: "DELETE" });
  },

  candidates(jobId: string): Promise<CandidatesResponse> {
    return request<CandidatesResponse>(`/jobs/${jobId}/candidates`);
  },

  replaceReview(jobId: string, revision: number, clips: ReviewClipInput[]): Promise<ReviewResponse> {
    return request<ReviewResponse>(`/jobs/${jobId}/review`, {
      method: "PUT",
      body: { revision, clips },
    });
  },

  // --- Exports -------------------------------------------------------------

  createExport(
    jobId: string,
    reviewRevision: number,
    resolutions: Resolution[],
    includeAudio: boolean,
    idempotencyKey: string,
  ): Promise<ExportRecord> {
    return request<ExportRecord>(`/jobs/${jobId}/exports`, {
      method: "POST",
      body: { reviewRevision, resolutions, includeAudio },
      idempotencyKey,
    });
  },

  getExport(jobId: string, exportId: string): Promise<ExportRecord> {
    return request<ExportRecord>(`/jobs/${jobId}/exports/${exportId}`);
  },

  cancelExport(jobId: string, exportId: string): Promise<ExportRecord> {
    return request<ExportRecord>(`/jobs/${jobId}/exports/${exportId}/cancel`, { method: "POST" });
  },

  retryExport(jobId: string, exportId: string, idempotencyKey: string): Promise<ExportRecord> {
    return request<ExportRecord>(`/jobs/${jobId}/exports/${exportId}/retry`, {
      method: "POST",
      idempotencyKey,
    });
  },

  exportFiles(jobId: string, exportId: string): Promise<ExportFilesResponse> {
    return request<ExportFilesResponse>(`/jobs/${jobId}/exports/${exportId}/files`);
  },

  downloadUrl(jobId: string, exportId: string): string {
    return `${BASE}/jobs/${jobId}/exports/${exportId}/download`;
  },

  /**
   * A ZIP of exactly the selected clips, assembled as it streams.
   *
   * `groups` comes from `encodeSelection`. This is a plain authenticated GET,
   * so the browser's own download manager handles it — no blob is buffered in
   * the page, which matters when a selection runs to gigabytes.
   */
  bundleUrl(jobId: string, exportId: string, groups: string[]): string {
    const query = groups.map((group) => `group=${encodeURIComponent(group)}`).join("&");
    return `${BASE}/jobs/${jobId}/exports/${exportId}/bundle?${query}`;
  },
};
