import { describe, expect, it } from "vitest";

import { encodeSelection, selectionBytes } from "../src/lib/selection";
import type { ExportFile, Resolution } from "../src/api/types";

function file(serial: number, resolution: Resolution = "original", sizeBytes = 1000): ExportFile {
  return {
    id: `${resolution}-${serial}`,
    serial,
    resolution,
    candidateId: `cand-${serial}`,
    sourceId: "source-1",
    sourceName: "SOURCE ONE",
    sourceFileName: "source-one.mp4",
    fileName: `clip-${serial}.mp4`,
    width: 1920,
    height: 1080,
    sizeBytes,
    durationUs: 3_000_000,
    sha256: "x".repeat(64),
    downloadUrl: `/api/v1/download/${serial}`,
    available: true,
  };
}

describe("encodeSelection", () => {
  it("collapses consecutive serials into ranges", () => {
    const chosen = [1, 2, 3, 4, 5].map((serial) => file(serial));
    expect(encodeSelection(chosen)).toEqual(["original:1-5"]);
  });

  it("keeps isolated serials on their own", () => {
    const chosen = [1, 3, 5].map((serial) => file(serial));
    expect(encodeSelection(chosen)).toEqual(["original:1,3,5"]);
  });

  it("mixes ranges and singles", () => {
    const chosen = [1, 2, 3, 7, 9, 10].map((serial) => file(serial));
    expect(encodeSelection(chosen)).toEqual(["original:1-3,7,9-10"]);
  });

  it("sorts out-of-order input", () => {
    const chosen = [9, 2, 1, 10, 3, 7].map((serial) => file(serial));
    expect(encodeSelection(chosen)).toEqual(["original:1-3,7,9-10"]);
  });

  it("emits one group per resolution", () => {
    const chosen = [
      file(1, "original"),
      file(2, "original"),
      file(1, "max720p"),
      file(4, "max720p"),
    ];
    expect(encodeSelection(chosen).sort()).toEqual(["max720p:1,4", "original:1-2"]);
  });

  it("deduplicates repeated files", () => {
    expect(encodeSelection([file(3), file(3), file(4)])).toEqual(["original:3-4"]);
  });

  it("returns nothing for an empty selection", () => {
    expect(encodeSelection([])).toEqual([]);
  });

  it("stays short for a whole 100-clip export", () => {
    const chosen = Array.from({ length: 100 }, (_, index) => file(index + 1));
    const encoded = encodeSelection(chosen);
    expect(encoded).toEqual(["original:1-100"]);
    // The point of the encoding: 100 UUIDs would not fit in a query string.
    expect(encoded.join("&").length).toBeLessThan(40);
  });

  it("stays manageable in the worst case of alternating serials", () => {
    const chosen = Array.from({ length: 50 }, (_, index) => file(index * 2 + 1));
    const encoded = encodeSelection(chosen).join("&");
    expect(encoded.startsWith("original:1,3,5")).toBe(true);
    expect(encoded.length).toBeLessThan(300);
  });
});

describe("selectionBytes", () => {
  it("sums the chosen file sizes", () => {
    expect(selectionBytes([file(1, "original", 100), file(2, "original", 250)])).toBe(350);
  });

  it("is zero for an empty selection", () => {
    expect(selectionBytes([])).toBe(0);
  });
});
