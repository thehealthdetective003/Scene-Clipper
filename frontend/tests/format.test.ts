import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatDuration,
  formatTimecode,
  secondsToUs,
  serialName,
  usToSeconds,
  warningLabel,
} from "../src/lib/format";

describe("timecode formatting", () => {
  it("renders HH:MM:SS.mmm", () => {
    expect(formatTimecode(0)).toBe("00:00:00.000");
    expect(formatTimecode(1_001_000)).toBe("00:00:01.001");
    expect(formatTimecode(3_661_500_000)).toBe("01:01:01.500");
  });

  it("never renders a negative position", () => {
    expect(formatTimecode(-5)).toBe("00:00:00.000");
  });

  it("round-trips seconds and microseconds", () => {
    expect(usToSeconds(6_000_000)).toBe(6);
    expect(secondsToUs(3.5)).toBe(3_500_000);
  });

  it("formats clip durations to two decimals", () => {
    expect(formatDuration(3_000_000)).toBe("3.00s");
    expect(formatDuration(5_972_633)).toBe("5.97s");
  });
});

describe("serial naming", () => {
  it("zero pads to four digits, matching the archive layout", () => {
    expect(serialName(1)).toBe("0001.mp4");
    expect(serialName(42)).toBe("0042.mp4");
    expect(serialName(9999)).toBe("9999.mp4");
  });
});

describe("byte formatting", () => {
  it("scales units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KiB");
    expect(formatBytes(20 * 1024 ** 3)).toBe("20 GiB");
  });
});

describe("warning labels", () => {
  it("explains known codes in plain language", () => {
    expect(warningLabel("gemini_key_not_configured")).toContain("local measurements");
  });

  it("falls back to the raw code when unknown", () => {
    expect(warningLabel("something_new")).toBe("something_new");
  });
});
