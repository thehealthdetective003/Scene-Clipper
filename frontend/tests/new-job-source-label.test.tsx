import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../src/api/client";
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
  error: null,
  createdAt: "2026-09-25T00:00:00Z",
  updatedAt: "2026-09-25T00:00:00Z",
};

describe("new-job source label", () => {
  afterEach(() => vi.restoreAllMocks());

  it("submits independent metadata for an uploaded source", async () => {
    vi.spyOn(api, "createUpload").mockResolvedValue({
      ...upload,
      verifiedOffsetBytes: 0,
      state: "created",
    });
    vi.spyOn(api, "putChunk").mockResolvedValue(1);
    vi.spyOn(api, "completeUpload").mockResolvedValue({ ...upload, state: "verifying" });
    vi.spyOn(api, "getUpload").mockResolvedValue(upload);
    const createJob = vi
      .spyOn(api, "createMultiSourceJob")
      .mockResolvedValue({ id: "job-1" } as never);
    const user = userEvent.setup();

    const { container } = render(
      <MemoryRouter initialEntries={["/jobs/new"]}>
        <Routes>
          <Route path="/jobs/new" element={<NewJobPage />} />
          <Route path="/jobs/:jobId" element={<div>Job opened</div>} />
        </Routes>
      </MemoryRouter>,
    );

    const picker = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(picker).not.toBeNull();
    await user.upload(picker!, new File([new Uint8Array([1])], "source.mp4", { type: "video/mp4" }));

    const sourceName = await screen.findByPlaceholderText("e.g. Driver Sphere");
    await user.type(sourceName, "Driver Sphere");
    await user.type(
      screen.getByPlaceholderText(/prioritize clear product shots/i),
      "Focus on the dashboard",
    );
    expect(screen.getByText(/13 \/ 48/i)).toBeInTheDocument();
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

  it("imports a video link and keeps the same job configuration flow", async () => {
    const remote = {
      ...upload,
      fileName: "Downloaded documentary.webm",
      sourceKind: "url" as const,
    };
    const createUrlUpload = vi.spyOn(api, "createUrlUpload").mockResolvedValue({
      ...remote,
      declaredSizeBytes: 0,
      verifiedOffsetBytes: 0,
      progressPercent: 0,
      state: "downloading",
    });
    vi.spyOn(api, "getUpload").mockResolvedValue(remote);
    const createJob = vi
      .spyOn(api, "createMultiSourceJob")
      .mockResolvedValue({ id: "job-url" } as never);
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/jobs/new"]}>
        <Routes>
          <Route path="/jobs/new" element={<NewJobPage />} />
          <Route path="/jobs/:jobId" element={<div>Job opened</div>} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("tab", { name: "Paste video links" }));
    await user.type(
      screen.getByRole("textbox", { name: "Video links" }),
      "https://www.youtube.com/watch?v=abc123",
    );
    await user.click(screen.getByRole("button", { name: "Add links" }));

    expect(await screen.findByText(/Downloaded documentary\.webm/)).toBeInTheDocument();
    expect(createUrlUpload).toHaveBeenCalledWith(
      "https://www.youtube.com/watch?v=abc123",
      expect.any(String),
    );

    await user.click(screen.getByRole("button", { name: "Analyse 1 source" }));
    await waitFor(() => expect(createJob).toHaveBeenCalled());
    expect(createJob.mock.calls[0]?.[0]).toEqual([
      { uploadId: remote.id, sourceName: null, contentPrompt: null },
    ]);
    expect(await screen.findByText("Job opened")).toBeInTheDocument();
  });

  it("adds several links to the same job and downloads them concurrently", async () => {
    const first = { ...upload, id: "url-1", fileName: "One.webm", sourceKind: "url" as const };
    const second = { ...upload, id: "url-2", fileName: "Two.webm", sourceKind: "url" as const };
    const pending = new Map<string, (value: Upload) => void>();
    vi.spyOn(api, "createUrlUpload").mockImplementation(async (url) => ({
      ...(url.endsWith("one") ? first : second),
      state: "downloading",
      progressPercent: 0,
    }));
    vi.spyOn(api, "getUpload").mockImplementation(
      (id) => new Promise<Upload>((resolve) => pending.set(id, resolve)),
    );
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/jobs/new"]}>
        <Routes>
          <Route path="/jobs/new" element={<NewJobPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("tab", { name: "Paste video links" }));
    await user.type(screen.getByRole("textbox", { name: "Video links" }), "https://x/one{enter}https://x/two");
    await user.click(screen.getByRole("button", { name: "Add links" }));

    await waitFor(() => expect(api.createUrlUpload).toHaveBeenCalledTimes(2));
    expect(pending.size).toBe(2);
    pending.get("url-1")?.(first);
    pending.get("url-2")?.(second);
    expect(await screen.findByText(/One\.webm/)).toBeInTheDocument();
    expect(await screen.findByText(/Two\.webm/)).toBeInTheDocument();
  });
});
