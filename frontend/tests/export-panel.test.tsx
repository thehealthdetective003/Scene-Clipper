import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../src/api/client";
import type { ExportFile, ExportRecord, Job } from "../src/api/types";
import { ExportPanel } from "../src/components/ExportPanel";

const job = {
  id: "job-1",
  reviewRevision: 4,
  video: { hasAudio: true },
  sources: [
    { id: "source-1", sourceName: "CAMERA ONE", fileName: "camera-one.mp4" },
  ],
} as Job;

const completedExport = {
  id: "export-1",
  state: "complete",
  reviewRevision: 4,
  progress: { phase: "complete", percent: 100, message: "Complete" },
} as unknown as ExportRecord;

const exportedFile: ExportFile = {
  id: "file-1",
  serial: 1,
  resolution: "original",
  candidateId: "candidate-1",
  sourceId: "source-1",
  sourceName: "CAMERA ONE",
  sourceFileName: "camera-one.mp4",
  fileName: "clip-0001-original.mp4",
  width: 1920,
  height: 1080,
  sizeBytes: 1024,
  durationUs: 3_000_000,
  sha256: "a".repeat(64),
  downloadUrl: "/api/v1/files/file-1/download",
  available: true,
};

describe("completed export downloads", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    delete window.showDirectoryPicker;
  });

  it("polls a queued export to completion when its live event is missed", async () => {
    const queuedExport = {
      ...completedExport,
      state: "queued",
      progress: { phase: "queued", percent: 0, message: "Queued for export." },
    } as ExportRecord;
    vi.spyOn(api, "getExport").mockResolvedValue(completedExport);
    vi.spyOn(api, "exportFiles").mockResolvedValue({
      files: [exportedFile],
      zipDownloadUrl: "/api/v1/jobs/job-1/exports/export-1/download",
      sourceBundles: [],
    });

    function PollingHarness() {
      const [record, setRecord] = useState<ExportRecord>(queuedExport);
      return (
        <ExportPanel
          job={job}
          selectedCount={1}
          activeExport={record}
          onStarted={setRecord}
        />
      );
    }

    render(<PollingHarness />);

    expect(await screen.findByRole("button", { name: /download zip/i })).toBeVisible();
    expect(api.getExport).toHaveBeenCalledWith("job-1", "export-1");
    expect(screen.queryByText("Queued for export.")).not.toBeInTheDocument();
  });

  it("offers individual files and an editable ZIP name for the current review", async () => {
    vi.spyOn(api, "exportFiles").mockResolvedValue({
      files: [exportedFile],
      zipDownloadUrl: "/api/v1/jobs/job-1/exports/export-1/download",
      sourceBundles: [],
    });

    render(
      <ExportPanel
        job={job}
        selectedCount={1}
        activeExport={completedExport}
        onStarted={vi.fn()}
      />,
    );

    expect(await screen.findByRole("button", { name: /choose folder & save 1 mp4/i })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "ZIP file name" })).toHaveValue("CAMERA ONE.zip");
    expect(screen.getByRole("button", { name: /download zip/i })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Export 1 clip" })).not.toBeInTheDocument();
  });

  it("passes the edited ZIP name to the server download", async () => {
    const user = userEvent.setup();
    let clickedHref = "";
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clickedHref = this.href;
    });
    vi.spyOn(api, "exportFiles").mockResolvedValue({
      files: [exportedFile],
      zipDownloadUrl: "/api/v1/jobs/job-1/exports/export-1/download",
      sourceBundles: [],
    });

    render(
      <ExportPanel job={job} selectedCount={1} activeExport={completedExport} onStarted={vi.fn()} />,
    );
    const name = await screen.findByRole("textbox", { name: "ZIP file name" });
    await user.clear(name);
    await user.type(name, "My edited archive.zip");
    await user.click(screen.getByRole("button", { name: /download zip/i }));

    expect(clickedHref).toContain("name=My%20edited%20archive.zip");
  });

  it("uses Explorer folder access to save individual MP4 files", async () => {
    const user = userEvent.setup();
    const write = vi.fn();
    const close = vi.fn();
    const createWritable = vi.fn().mockResolvedValue({ write, close });
    const getFileHandle = vi.fn().mockResolvedValue({ createWritable });
    window.showDirectoryPicker = vi.fn().mockResolvedValue({ getFileHandle } as never);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        body: null,
        blob: () => Promise.resolve(new Blob(["mp4"])),
      }),
    );
    vi.spyOn(api, "exportFiles").mockResolvedValue({
      files: [exportedFile],
      zipDownloadUrl: "/api/v1/jobs/job-1/exports/export-1/download",
      sourceBundles: [],
    });

    render(
      <ExportPanel job={job} selectedCount={1} activeExport={completedExport} onStarted={vi.fn()} />,
    );
    await user.click(await screen.findByRole("button", { name: /choose folder & save 1 mp4/i }));

    await waitFor(() =>
      expect(getFileHandle).toHaveBeenCalledWith(exportedFile.fileName, { create: true }),
    );
    expect(write).toHaveBeenCalled();
    expect(close).toHaveBeenCalled();
  });

  it("offers source-named archives for a multi-source export", async () => {
    vi.spyOn(api, "exportFiles").mockResolvedValue({
      files: [exportedFile],
      zipDownloadUrl: "/api/v1/jobs/job-1/exports/export-1/download",
      sourceBundles: [
        {
          sourceId: "source-1",
          sourceName: "CAMERA ONE",
          sourceFileName: "camera-one.mp4",
          fileName: "CAMERA-ONE.zip",
          fileCount: 1,
          sizeBytes: 1024,
          available: true,
          downloadUrl: "/api/v1/source-1/download",
        },
      ],
    });

    render(
      <ExportPanel
        job={{
          ...job,
          sources: [
            ...job.sources,
            { ...job.sources[0]!, id: "source-2", sourceName: "CAMERA TWO" },
          ],
        }}
        selectedCount={1}
        activeExport={completedExport}
        onStarted={vi.fn()}
      />,
    );

    expect(await screen.findByText("Separate ZIP for each source")).toBeVisible();
    expect(screen.getByText("CAMERA-ONE.zip")).toBeVisible();
  });

  it("offers a fresh export after the review changes", async () => {
    vi.spyOn(api, "exportFiles").mockResolvedValue({
      files: [],
      zipDownloadUrl: null,
      sourceBundles: [],
    });

    render(
      <ExportPanel
        job={{ ...job, reviewRevision: 5 }}
        selectedCount={2}
        activeExport={completedExport}
        onStarted={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Export 2 clips" })).toBeInTheDocument();
    await waitFor(() => expect(api.exportFiles).toHaveBeenCalledWith("job-1", "export-1"));
  });
});
