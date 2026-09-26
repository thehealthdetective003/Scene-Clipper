import type { ExportFile, Resolution } from "../api/types";

/**
 * Encode a set of export files as the compact `resolution:serial-ranges` spec
 * the bundle endpoint takes.
 *
 * Serials are small, gapless, server-issued integers, so a whole 100-clip
 * export compresses to something like `original:1-100` — a few dozen
 * characters. The same selection as file ids would be several kilobytes of
 * query string, past what a browser or proxy will carry.
 */
export function encodeSelection(files: ExportFile[]): string[] {
  const byResolution = new Map<Resolution, number[]>();
  for (const file of files) {
    const serials = byResolution.get(file.resolution);
    if (serials) serials.push(file.serial);
    else byResolution.set(file.resolution, [file.serial]);
  }

  const groups: string[] = [];
  for (const [resolution, serials] of byResolution) {
    const ordered = [...new Set(serials)].sort((a, b) => a - b);
    const first = ordered[0];
    if (first === undefined) continue;

    const spans: string[] = [];
    let start = first;
    let previous = first;

    for (const serial of ordered.slice(1)) {
      if (serial === previous + 1) {
        previous = serial;
        continue;
      }
      spans.push(start === previous ? `${start}` : `${start}-${previous}`);
      start = serial;
      previous = serial;
    }
    spans.push(start === previous ? `${start}` : `${start}-${previous}`);
    groups.push(`${resolution}:${spans.join(",")}`);
  }
  return groups;
}

/** The total size of a selection, for the "Download 9 · 412 MB" affordance. */
export function selectionBytes(files: ExportFile[]): number {
  return files.reduce((total, file) => total + file.sizeBytes, 0);
}
