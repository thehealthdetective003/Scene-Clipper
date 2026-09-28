import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../src/api/client";
import type { Upload } from "../src/api/types";
import { NewJobPage } from "../src/pages/NewJobPage";

const upload: Upload = {
  id: "upload-1",
  fileName: "source.mp4",
  declaredSizeBytes: 1,
  verifiedOffsetBytes: 1,
  chunkSizeBytes: 1,
  state: "ready",
  sha256: "a".repeat(64),
  progressPercent: 100,
  sourceKind: "file",
  suggestedSourceName: null,
  error: null,
  createdAt: "2026-09-25T00:00:00Z",
  updatedAt: "2026-09-25T00:00:00Z",
};

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/jobs/new"]}>
      <Routes>
        <Route path="/jobs/new" element={<NewJobPage />} />
        <Route path="/jobs/:jobId" element={<div>Job opened</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function mockFilePreparation() {
  vi.spyOn(api, "createUpload").mockResolvedValue({
    ...upload,
    verifiedOffsetBytes: 0,
    state: "created",
  });
  vi.spyOn(api, "putChunk").mockResolvedValue(1);
  vi.spyOn(api, "completeUpload").mockResolvedValue({ ...upload, state: "verifying" });
  vi.spyOn(api, "getUpload").mockResolvedValue(upload);
}

function mockReadyLink(remote: Upload) {
  vi.spyOn(api, "createUrlUpload").mockResolvedValue({
    ...remote,
    declaredSizeBytes: 0,
    verifiedOffsetBytes: 0,
    progressPercent: 0,
    state: "downloading",
  });
  vi.spyOn(api, "getUpload").mockResolvedValue(remote);
}

describe("new-job source rows", () => {
  afterEach(() => vi.restoreAllMocks());

  it("lets a file source be named before preparation and edited afterward", async () => {
    mockFilePreparation();
    const createJob = vi
      .spyOn(api, "createMultiSourceJob")
      .mockResolvedValue({ id: "job-file" } as never);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Remove source 1" }));
    await user.click(screen.getByRole("button", { name: "Add a video file" }));
    const sourceName = screen.getByRole("textbox", { name: /Source name for source 1/i });
    await user.type(sourceName, "Draft name");
    await user.upload(
      screen.getByLabelText("Video file for source 1"),
      new File([new Uint8Array([1])], "source.mp4", { type: "video/mp4" }),
    );

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Analyse 1 source" })).toBeEnabled(),
    );
    await user.clear(sourceName);
    await user.type(sourceName, "Driver Sphere");
    await user.type(
      screen.getByRole("textbox", { name: /Instruction for source 1/i }),
      "Focus on the dashboard",
    );
    await user.click(screen.getByRole("button", { name: "Analyse 1 source" }));

    await waitFor(() => expect(createJob).toHaveBeenCalled());
    expect(createJob.mock.calls[0]?.[0]).toEqual([
      {
        uploadId: "upload-1",
        sourceName: "Driver Sphere",
        contentPrompt: "Focus on the dashboard",
      },
    ]);
    expect(createJob.mock.calls[0]?.[2]).toBe(false);
    expect(await screen.findByText("Job opened")).toBeInTheDocument();
  });

  it("pairs a link with its CJK source name before and after download", async () => {
    const remote = {
      ...upload,
      id: "url-1",
      fileName: "Downloaded documentary.webm",
      sourceKind: "url" as const,
    };
    mockReadyLink(remote);
    const createJob = vi
      .spyOn(api, "createMultiSourceJob")
      .mockResolvedValue({ id: "job-url" } as never);
    const user = userEvent.setup();
    renderPage();

    await user.type(
      screen.getByRole("textbox", { name: "Video link for source 1" }),
      "https://www.youtube.com/watch?v=abc123",
    );
    const sourceName = screen.getByRole("textbox", { name: /Source name for source 1/i });
    await user.type(sourceName, "环球时报 Draft");
    await user.click(screen.getByRole("button", { name: "Download and prepare video" }));

    expect(await screen.findByText("Downloaded documentary.webm")).toBeInTheDocument();
    await user.clear(sourceName);
    await user.type(sourceName, "环球时报 Global Times");
    await user.click(screen.getByRole("button", { name: "Analyse 1 source" }));

    await waitFor(() => expect(createJob).toHaveBeenCalled());
    expect(createJob.mock.calls[0]?.[0]).toEqual([
      {
        uploadId: "url-1",
        sourceName: "环球时报 Global Times",
        contentPrompt: null,
      },
    ]);
    expect(await screen.findByText("Job opened")).toBeInTheDocument();
  });

  it("fills an untouched URL source name from the downloaded video channel", async () => {
    const remote = {
      ...upload,
      id: "url-channel",
      fileName: "Channel video.webm",
      sourceKind: "url" as const,
      suggestedSourceName: "Global Times",
    };
    mockReadyLink(remote);
    const user = userEvent.setup();
    renderPage();

    await user.type(
      screen.getByRole("textbox", { name: "Video link for source 1" }),
      "https://www.youtube.com/watch?v=channel",
    );
    const sourceName = screen.getByRole("textbox", { name: /Source name for source 1/i });
    expect(sourceName).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Download and prepare video" }));

    await screen.findByText("Channel video.webm");
    expect(sourceName).toHaveValue("Global Times");
    expect(screen.getByText(/Filled from the video channel; you can edit it/i)).toBeInTheDocument();

    await user.clear(sourceName);
    await user.type(sourceName, "My edited source");
    expect(sourceName).toHaveValue("My edited source");
  });

  it("does not overwrite a source name typed while the URL is downloading", async () => {
    const remote = {
      ...upload,
      id: "url-slow-channel",
      fileName: "Slow video.webm",
      sourceKind: "url" as const,
      suggestedSourceName: "Downloaded Channel",
    };
    let finishDownload: ((value: Upload) => void) | undefined;
    vi.spyOn(api, "createUrlUpload").mockResolvedValue({
      ...remote,
      state: "downloading",
      progressPercent: 0,
    });
    vi.spyOn(api, "getUpload").mockImplementation(
      () => new Promise<Upload>((resolve) => (finishDownload = resolve)),
    );
    const user = userEvent.setup();
    renderPage();

    await user.type(
      screen.getByRole("textbox", { name: "Video link for source 1" }),
      "https://www.youtube.com/watch?v=slow",
    );
    await user.click(screen.getByRole("button", { name: "Download and prepare video" }));
    await waitFor(() => expect(finishDownload).toBeDefined());
    const sourceName = screen.getByRole("textbox", { name: /Source name for source 1/i });
    await user.type(sourceName, "My live edit");
    finishDownload?.(remote);

    await screen.findByText("Slow video.webm");
    expect(sourceName).toHaveValue("My live edit");
  });

  it("keeps pasted link order when the second download finishes first", async () => {
    const first = {
      ...upload,
      id: "url-1",
      fileName: "First finished later.webm",
      sourceKind: "url" as const,
    };
    const second = {
      ...upload,
      id: "url-2",
      fileName: "Second finished first.webm",
      sourceKind: "url" as const,
    };
    const pending = new Map<string, (value: Upload) => void>();
    vi.spyOn(api, "createUrlUpload").mockImplementation(async (url) => ({
      ...(url.endsWith("first") ? first : second),
      state: "downloading",
      progressPercent: 0,
    }));
    vi.spyOn(api, "getUpload").mockImplementation(
      (id) => new Promise<Upload>((resolve) => pending.set(id, resolve)),
    );
    const createJob = vi
      .spyOn(api, "createMultiSourceJob")
      .mockResolvedValue({ id: "job-ordered" } as never);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add another video link" }));
    await user.type(
      screen.getByRole("textbox", { name: "Video link for source 1" }),
      "https://videos.test/first",
    );
    await user.type(
      screen.getByRole("textbox", { name: "Video link for source 2" }),
      "https://videos.test/second",
    );
    await user.type(
      screen.getByRole("textbox", { name: /Source name for source 1/i }),
      "First source",
    );
    await user.type(
      screen.getByRole("textbox", { name: /Source name for source 2/i }),
      "Second source",
    );
    await user.click(screen.getByRole("button", { name: "Prepare all links" }));

    await waitFor(() => expect(pending.size).toBe(2));
    pending.get("url-2")?.(second);
    expect(
      await within(screen.getByTestId("source-row-2")).findByText("Second finished first.webm"),
    ).toBeInTheDocument();
    expect(within(screen.getByTestId("source-row-1")).queryByText("Ready")).not.toBeInTheDocument();

    pending.get("url-1")?.(first);
    expect(
      await within(screen.getByTestId("source-row-1")).findByText("First finished later.webm"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Analyse 2 sources" }));

    await waitFor(() => expect(createJob).toHaveBeenCalled());
    expect(createJob.mock.calls[0]?.[0]).toEqual([
      { uploadId: "url-1", sourceName: "First source", contentPrompt: null },
      { uploadId: "url-2", sourceName: "Second source", contentPrompt: null },
    ]);
  });

  it("supports a mixed link and file job in the visible row order", async () => {
    const remote = {
      ...upload,
      id: "url-first",
      fileName: "remote.webm",
      sourceKind: "url" as const,
    };
    mockReadyLink(remote);
    vi.spyOn(api, "createUpload").mockResolvedValue({
      ...upload,
      id: "file-second",
      verifiedOffsetBytes: 0,
      state: "created",
    });
    vi.spyOn(api, "putChunk").mockResolvedValue(1);
    vi.spyOn(api, "completeUpload").mockResolvedValue({
      ...upload,
      id: "file-second",
      state: "verifying",
    });
    vi.mocked(api.getUpload).mockImplementation(async (id) =>
      id === "url-first" ? remote : { ...upload, id: "file-second" },
    );
    const createJob = vi
      .spyOn(api, "createMultiSourceJob")
      .mockResolvedValue({ id: "job-mixed" } as never);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add a video file" }));
    await user.type(
      screen.getByRole("textbox", { name: "Video link for source 1" }),
      "https://videos.test/remote",
    );
    await user.type(
      screen.getByRole("textbox", { name: /Source name for source 1/i }),
      "Remote first",
    );
    await user.type(
      screen.getByRole("textbox", { name: /Source name for source 2/i }),
      "File second",
    );
    await user.upload(
      screen.getByLabelText("Video file for source 2"),
      new File([new Uint8Array([1])], "local.mp4", { type: "video/mp4" }),
    );
    await user.click(screen.getByRole("button", { name: "Download and prepare video" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Analyse 2 sources" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Analyse 2 sources" }));

    await waitFor(() => expect(createJob).toHaveBeenCalled());
    expect(createJob.mock.calls[0]?.[0]).toEqual([
      { uploadId: "url-first", sourceName: "Remote first", contentPrompt: null },
      { uploadId: "file-second", sourceName: "File second", contentPrompt: null },
    ]);
  });

  it("requires a changed link to be prepared again without losing its name", async () => {
    const first = {
      ...upload,
      id: "url-old",
      fileName: "old.webm",
      sourceKind: "url" as const,
    };
    const replacement = { ...first, id: "url-new", fileName: "new.webm" };
    vi.spyOn(api, "createUrlUpload")
      .mockResolvedValueOnce({ ...first, state: "downloading" })
      .mockResolvedValueOnce({ ...replacement, state: "downloading" });
    vi.spyOn(api, "getUpload")
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(replacement);
    const deleteUpload = vi.spyOn(api, "deleteUpload").mockResolvedValue();
    const user = userEvent.setup();
    renderPage();

    const link = screen.getByRole("textbox", { name: "Video link for source 1" });
    const name = screen.getByRole("textbox", { name: /Source name for source 1/i });
    await user.type(link, "https://videos.test/old");
    await user.type(name, "Persistent source name");
    await user.click(screen.getByRole("button", { name: "Download and prepare video" }));
    await screen.findByText("old.webm");

    await user.clear(link);
    await user.type(link, "https://videos.test/new");
    expect(screen.getByText("Link changed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Analyse 1 source" })).toBeDisabled();
    expect(name).toHaveValue("Persistent source name");
    await user.click(screen.getByRole("button", { name: "Prepare updated link" }));

    await screen.findByText("new.webm");
    expect(screen.getByRole("button", { name: "Analyse 1 source" })).toBeEnabled();
    await waitFor(() => expect(deleteUpload).toHaveBeenCalledWith("url-old"));
  });

  it("shows the backend field error for the affected source", async () => {
    const remote = {
      ...upload,
      id: "url-error",
      fileName: "source.webm",
      sourceKind: "url" as const,
    };
    mockReadyLink(remote);
    vi.spyOn(api, "createMultiSourceJob").mockRejectedValue(
      new ApiError(422, {
        code: "invalid_request",
        message: "The request failed validation.",
        retryable: false,
        requestId: "request-1",
        details: {
          fields: [
            {
              field: "body.sources.0.sourceName",
              message: "Value error, The source name contains an unsupported character.",
            },
          ],
        },
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.type(
      screen.getByRole("textbox", { name: "Video link for source 1" }),
      "https://videos.test/source",
    );
    await user.click(screen.getByRole("button", { name: "Download and prepare video" }));
    await screen.findByText("source.webm");
    await user.click(screen.getByRole("button", { name: "Analyse 1 source" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Source 1 name: The source name contains an unsupported character.",
    );
  });
});
