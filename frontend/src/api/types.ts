/**
 * API contracts, mirroring the normative schemas in spec section 9.
 * All media positions are integer microseconds.
 */

export type UploadState = "created" | "uploading" | "verifying" | "ready" | "failed";

export type JobState =
  | "uploaded"
  | "probing"
  | "detecting"
  | "ranking"
  | "review-ready"
  | "exporting"
  | "complete"
  | "failed"
  | "cancelled";

export type ExportState = "queued" | "exporting" | "complete" | "failed" | "cancelled";

export type Resolution = "original" | "max1080p" | "max720p";

export type SourceLabelFontPreset =
  | "bebas-neue"
  | "anton"
  | "oswald-semibold"
  | "roboto-condensed-bold";

export interface SourceLabelStyle {
  fontPreset: SourceLabelFontPreset;
  fillColor: string;
  outlineColor: string;
  sizePercent: number;
}

export interface SourceLabelSettings extends SourceLabelStyle {
  updatedAt: string | null;
}

export interface SourceLabel {
  text: string;
  style: SourceLabelStyle;
}

export type ScoringSource = "contact-sheet" | "proxy-video" | "local-fallback";

export type CacheStatus = "none" | "partial" | "complete";

export interface JobError {
  phase: string;
  code: string;
  message: string;
  retryable: boolean;
  occurredAt: string;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    retryable: boolean;
    requestId: string;
    details: Record<string, unknown>;
  };
}

export interface Upload {
  id: string;
  fileName: string;
  declaredSizeBytes: number;
  verifiedOffsetBytes: number;
  chunkSizeBytes: number;
  state: UploadState;
  sha256: string | null;
  progressPercent: number;
  error: JobError | null;
  createdAt: string;
  updatedAt: string;
}

export interface ExportSummary {
  id: string;
  state: ExportState;
  createdAt: string;
  completedAt: string | null;
}

export interface JobSummary {
  id: string;
  sourceFileName: string;
  state: JobState;
  progressPercent: number;
  targetClipCount: number;
  selectedCount: number;
  latestExport: ExportSummary | null;
  createdAt: string;
  updatedAt: string;
}

export interface JobList {
  items: JobSummary[];
  nextCursor: string | null;
}

export interface AnalysisUsage {
  model: string | null;
  requestCap: number;
  requestsUsed: number;
  coarseRequests: number;
  fineRequests: number;
  proxyVideoRequests: number;
  proxyVideoCandidates: number;
  providerFileOperations: number;
  imageBytesSent: number;
  proxyVideoBytesSent: number;
  proxyVideoSecondsSent: number;
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  cacheStatus: CacheStatus;
  localFallbackUsed: boolean;
  fallbackReason: string | null;
}

export interface Job {
  id: string;
  uploadId: string;
  state: JobState;
  targetClipCount: number;
  contentPrompt: string | null;
  sourceLabel: SourceLabel | null;
  useGemini: boolean;
  video: {
    sha256: string;
    durationUs: number;
    width: number;
    height: number;
    averageFrameRate: string;
    hasAudio: boolean;
  } | null;
  progress: { phase: JobState; percent: number; message: string };
  eligibleCount: number | null;
  selectedCount: number;
  reviewRevision: number;
  latestExport: ExportSummary | null;
  partialResultReason: string | null;
  warnings: string[];
  usage: AnalysisUsage;
  error: JobError | null;
  createdAt: string;
  updatedAt: string;
}

export interface TransitionBoundary {
  startUs: number;
  endUs: number;
  kinds: Array<"hard-cut" | "fade" | "dissolve">;
}

export interface CandidateShot {
  id: string;
  jobId: string;
  shotNumber: number;
  sourceStartUs: number;
  sourceEndUs: number;
  safeStartUs: number;
  safeEndUs: number;
  usableDurationUs: number;
  recommendedStartUs: number;
  recommendedEndUs: number;
  incomingBoundary: TransitionBoundary | null;
  outgoingBoundary: TransitionBoundary | null;
  rank: number;
  score: number;
  confidence: number;
  reason: string;
  scoringSource: ScoringSource;
  cacheStatus: CacheStatus;
  promptRelevanceEvaluated: boolean;
  thumbnailUrl: string;
  previewUrl: string;
  /** `null` means nobody assessed it — not the same as "no person is present". */
  humanPresent: boolean | null;
  /** `model` (Gemini judged the frames) or `local` (OpenCV screen). */
  humanSource: "model" | "local" | null;
  productVisible: boolean | null;
  productProminence: number | null;
  /** Set when the shot was passed over for automatic selection. */
  excludedReason: "human_present" | "product_absent" | null;
}

export interface SelectedClip {
  id: string;
  candidateId: string;
  order: number;
  startUs: number;
  endUs: number;
  durationUs: number;
}

export interface CandidatesResponse {
  candidates: CandidateShot[];
  selectedClips: SelectedClip[];
  reviewRevision: number;
}

export interface ReviewClipInput {
  candidateId: string;
  order: number;
  startUs: number;
  endUs: number;
}

export interface ReviewResponse {
  reviewRevision: number;
  clips: SelectedClip[];
}

export interface ExportRecord {
  id: string;
  jobId: string;
  state: ExportState;
  reviewRevision: number;
  resolutions: Resolution[];
  includeAudio: boolean;
  progress: { percent: number; message: string };
  error: JobError | null;
  manifestAvailable: boolean;
  downloadAvailable: boolean;
  createdAt: string;
  updatedAt: string;
  completedAt: string | null;
}

export interface ExportFile {
  id: string;
  serial: number;
  resolution: Resolution;
  candidateId: string;
  fileName: string;
  width: number;
  height: number;
  sizeBytes: number;
  durationUs: number;
  sha256: string;
  downloadUrl: string;
  /** False for exports produced before individual files were published. */
  available: boolean;
}

export interface ExportFilesResponse {
  files: ExportFile[];
  zipDownloadUrl: string | null;
}

/** Key health. `exhausted` and `invalid` are the states shown in red. */
export type GeminiKeyStatus = "active" | "exhausted" | "invalid" | "unavailable" | "disabled";

/**
 * One credential in the failover pool. Carries health only — the key itself is
 * never returned by the API, not even masked (spec 5.2).
 */
export interface GeminiKey {
  id: string;
  label: string;
  position: number;
  status: GeminiKeyStatus;
  available: boolean;
  lastErrorCode: string | null;
  lastErrorAt: string | null;
  lastSuccessAt: string | null;
  cooldownUntil: string | null;
  requestsSucceeded: number;
  requestsFailed: number;
  createdAt: string | null;
}

export interface GeminiSettings {
  configured: boolean;
  model: string | null;
  requestCap: number;
  updatedAt: string | null;
  keys: GeminiKey[];
  maxKeys: number;
  anyAvailable: boolean;
}

export interface GeminiKeyTestResult {
  valid: boolean;
  result: string;
  message: string;
}

export interface SessionInfo {
  authenticated: boolean;
  csrfToken: string | null;
}

/** SSE envelope (spec 8.4). */
export interface JobEvent {
  sequence: number;
  type: "job.updated" | "candidates.ready" | "export.ready" | "job.failed" | "heartbeat";
  jobId: string;
  occurredAt: string;
  payload: Record<string, unknown>;
}
