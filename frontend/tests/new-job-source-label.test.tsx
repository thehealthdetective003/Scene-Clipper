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
  error: null,
  createdAt: "2026-09-25T00:00:00Z",
  updatedAt: "2026-09-25T00:00:00Z",
};

describe("new-job source label", () => {
  afterEach(() => vi.restoreAllMocks());

  it("submits the optional source name after upload verification", async () => {
    vi.spyOn(api, "createUpload").mockResolvedValue({
      ...upload,
      verifiedOffsetBytes: 0,
      state: "created",
    });
    vi.spyOn(api, "putChunk").mockResolvedValue(1);
    vi.spyOn(api, "completeUpload").mockResolvedValue({ ...upload, state: "verifying" });
    vi.spyOn(api, "getUpload").mockResolvedValue(upload);
    const createJob = vi.spyOn(api, "createJob").mockResolvedValue({ id: "job-1" } as never);
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
    expect(screen.getByText(/13 \/ 48 characters/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start analysis" }));

    await waitFor(() => expect(createJob).toHaveBeenCalled());
    expect(createJob.mock.calls[0]?.[3]).toBe("Driver Sphere");
    expect(await screen.findByText("Job opened")).toBeInTheDocument();
  });
});
