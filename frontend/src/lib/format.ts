/** Formatting helpers. All media positions are integer microseconds. */

const US_PER_SECOND = 1_000_000;

export function usToSeconds(us: number): number {
  return us / US_PER_SECOND;
}

export function secondsToUs(seconds: number): number {
  return Math.round(seconds * US_PER_SECOND);
}

/** `HH:MM:SS.mmm`, matching the labels drawn on contact sheets. */
export function formatTimecode(us: number): string {
  const totalMs = Math.max(0, Math.floor(us / 1000));
  const hours = Math.floor(totalMs / 3_600_000);
  const minutes = Math.floor((totalMs % 3_600_000) / 60_000);
  const seconds = Math.floor((totalMs % 60_000) / 1000);
  const millis = totalMs % 1000;
  return (
    `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:` +
    `${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`
  );
}

export function formatDuration(us: number): string {
  return `${(us / US_PER_SECOND).toFixed(2)}s`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
}

export function formatRate(bytesPerSecond: number): string {
  return `${formatBytes(bytesPerSecond)}/s`;
}

export function formatEta(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "estimating…";
  if (seconds < 60) return `${Math.ceil(seconds)}s remaining`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.ceil(seconds % 60)}s remaining`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m remaining`;
}

export function serialName(index: number): string {
  return `${String(index).padStart(4, "0")}.mp4`;
}

const SCORING_SOURCE_LABELS: Record<string, string> = {
  "contact-sheet": "Ranked from frames",
  "proxy-video": "Ranked from a short proxy clip",
  "local-fallback": "Ranked locally",
};

export function scoringSourceLabel(source: string): string {
  return SCORING_SOURCE_LABELS[source] ?? source;
}

const CACHE_STATUS_LABELS: Record<string, string> = {
  none: "Freshly analyzed",
  partial: "Partly reused from cache",
  complete: "Reused from cache",
};

export function cacheStatusLabel(status: string): string {
  return CACHE_STATUS_LABELS[status] ?? status;
}

const WARNING_LABELS: Record<string, string> = {
  gemini_key_not_configured:
    "No Gemini key was configured, so clips were ranked with local measurements only.",
  gemini_key_rejected:
    "A Gemini key was rejected, so ranking moved to the next key. Check Settings.",
  gemini_key_exhausted:
    "A Gemini key hit its limit, so ranking moved to the next key. Check Settings.",
  all_gemini_keys_unavailable:
    "Every Gemini key is exhausted or rejected, so clips were ranked with local measurements.",
  gemini_fallback: "Gemini was unavailable, so ranking fell back to local measurements.",
  human_present_shots_excluded:
    "Shots with a person in them were held back from automatic selection. You can still pick them by hand.",
  human_check_incomplete:
    "Some shots could not be checked for people, so they were not treated as product-only.",
  request_cap_reached: "The Gemini request cap was reached; remaining clips were ranked locally.",
  coarse_capacity_limited:
    "The request cap did not cover every candidate; the rest were ranked locally.",
  fewer_eligible_than_requested: "Fewer usable shots were found than the number you requested.",
  variable_frame_rate: "This source has a variable frame rate; cuts stay frame-accurate.",
  hdr_source_tonemapped_on_export: "This source is HDR; exports are converted for compatibility.",
  average_frame_rate_estimated: "The frame rate had to be estimated from the container.",
};

export function warningLabel(code: string): string {
  return WARNING_LABELS[code] ?? code;
}

export function stateLabel(state: string): string {
  const labels: Record<string, string> = {
    uploaded: "Queued",
    probing: "Reading metadata",
    detecting: "Detecting shots",
    ranking: "Ranking clips",
    "review-ready": "Ready for review",
    exporting: "Exporting",
    complete: "Complete",
    failed: "Failed",
    cancelled: "Cancelled",
    queued: "Queued",
  };
  return labels[state] ?? state;
}
