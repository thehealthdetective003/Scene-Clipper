import { expect, test } from "@playwright/test";

test("renders live per-source analysis activity in the browser", async ({ page }) => {
  const now = new Date().toISOString();
  const video = {
    sha256: "a".repeat(64),
    durationUs: 125_000_000,
    width: 1920,
    height: 1080,
    averageFrameRate: "30/1",
    hasAudio: true,
  };
  const source = (id: string, order: number, name: string, phase: string, percent: number, message: string) => ({
    id,
    uploadId: `upload-${id}`,
    order,
    fileName: `${id}.mp4`,
    sourceKind: "url",
    sourceName: name,
    sourceLabel: null,
    contentPrompt: null,
    video,
    state: "processing",
    progress: { phase, percent, message },
    detectedCount: phase === "detecting" ? null : 18,
    eligibleCount: phase === "detecting" ? null : 12,
    updatedAt: now,
  });

  await page.route(/\/api\/v1\/jobs\/live-progress-job$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        id: "live-progress-job",
        uploadId: "upload-source-1",
        state: "detecting",
        targetClipCount: 20,
        contentPrompt: null,
        sourceLabel: null,
        useGemini: false,
        rankingEnabled: false,
        sources: [
          source("source-1", 0, "FIRST SOURCE", "detecting", 47, "Scanning video frames · 74% complete."),
          source("source-2", 1, "SECOND SOURCE", "measuring", 71, "Measuring candidate 6 of 12."),
        ],
        video,
        progress: { phase: "detecting", percent: 56, message: "Processing 2 sources." },
        eligibleCount: null,
        selectedCount: 0,
        reviewRevision: 0,
        latestExport: null,
        partialResultReason: null,
        warnings: [],
        usage: {
          model: null,
          requestCap: 0,
          requestsUsed: 0,
          coarseRequests: 0,
          fineRequests: 0,
          proxyVideoRequests: 0,
          proxyVideoCandidates: 0,
          providerFileOperations: 0,
          imageBytesSent: 0,
          proxyVideoBytesSent: 0,
          proxyVideoSecondsSent: 0,
          inputTokens: null,
          outputTokens: null,
          totalTokens: null,
          cacheStatus: "none",
          localFallbackUsed: false,
          fallbackReason: null,
        },
        error: null,
        createdAt: now,
        updatedAt: now,
      }),
    });
  });

  await page.goto("/jobs/live-progress-job");

  await expect(page.getByRole("region", { name: "Live analysis activity" })).toBeVisible();
  await expect(page.getByText("2 sources are working in parallel.")).toBeVisible();
  await expect(page.getByTestId("analysis-source-1")).toContainText("FIRST SOURCE");
  await expect(page.getByTestId("analysis-source-1")).toContainText("47%");
  await expect(page.getByTestId("analysis-source-2")).toContainText("Measuring candidate 6 of 12");
  await expect(page.getByTestId("analysis-source-2")).toContainText("18 scenes found");
});
