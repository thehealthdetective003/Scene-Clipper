import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Job } from "../src/api/types";
import { AnalysisActivity } from "../src/components/AnalysisActivity";

function source(
  id: string,
  name: string,
  phase: Job["sources"][number]["progress"]["phase"],
  percent: number,
  message: string,
) {
  return {
    id,
    uploadId: `upload-${id}`,
    order: Number(id.slice(-1)) - 1,
    fileName: `${id}.mp4`,
    sourceKind: "url" as const,
    sourceName: name,
    sourceLabel: null,
    contentPrompt: null,
    video: {
      sha256: "a".repeat(64),
      durationUs: 125_000_000,
      width: 1920,
      height: 1080,
      averageFrameRate: "30/1",
      hasAudio: true,
    },
    state: phase === "ready" ? "ready" : "processing",
    progress: { phase, percent, message },
    detectedCount: phase === "detecting" ? null : 18,
    eligibleCount: phase === "detecting" ? null : 12,
    updatedAt: new Date().toISOString(),
  };
}

const job = {
  id: "job-1",
  state: "detecting",
  sources: [
    source("source-1", "FIRST SOURCE", "detecting", 47, "Scanning video frames · 74% complete."),
    source("source-2", "SECOND SOURCE", "measuring", 71, "Measuring candidate 6 of 12."),
  ],
  progress: { phase: "detecting", percent: 56, message: "Processing sources." },
} as Job;

describe("AnalysisActivity", () => {
  it("shows real per-source tasks, percentages, and discovered-scene counts", () => {
    render(<AnalysisActivity job={job} connection="open" />);

    expect(screen.getByRole("region", { name: "Live analysis activity" })).toBeVisible();
    expect(screen.getByText("2 sources are working in parallel.")).toBeVisible();
    expect(screen.getByTestId("analysis-source-1")).toHaveTextContent("FIRST SOURCE");
    expect(screen.getByTestId("analysis-source-1")).toHaveTextContent("Finding scenes");
    expect(screen.getByTestId("analysis-source-1")).toHaveTextContent("47%");
    expect(screen.getByTestId("analysis-source-2")).toHaveTextContent("Measuring candidate 6 of 12");
    expect(screen.getByTestId("analysis-source-2")).toHaveTextContent("18 scenes found");
    expect(screen.getByTestId("analysis-source-2")).toHaveTextContent("12 usable");
  });

  it("marks completed sources without hiding sources still working", () => {
    const updated = {
      ...job,
      sources: [
        { ...job.sources[0]!, state: "ready", progress: { phase: "ready", percent: 100, message: "Analysis complete." } },
        job.sources[1]!,
      ],
    } as Job;

    render(<AnalysisActivity job={updated} connection="reconnecting" />);

    expect(screen.getByText("1 of 2 sources ready")).toBeVisible();
    expect(screen.getByTestId("analysis-source-1")).toHaveTextContent("Complete");
    expect(screen.getByTestId("analysis-source-2")).toHaveTextContent("Measuring quality");
    expect(screen.getByText("Refreshing")).toBeVisible();
  });
});
