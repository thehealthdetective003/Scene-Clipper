import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../src/api/client";
import { SourceLabelSettingsPanel } from "../src/components/SourceLabelSettingsPanel";

const defaults = {
  fontPreset: "bebas-neue" as const,
  fillColor: "#FFFFFF",
  outlineColor: "#000000",
  sizePercent: 4,
  updatedAt: null,
};

describe("SourceLabelSettingsPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("loads settings, previews changes, and saves the full style", async () => {
    vi.spyOn(api, "sourceLabelSettings").mockResolvedValue(defaults);
    const save = vi
      .spyOn(api, "saveSourceLabelSettings")
      .mockImplementation(async (style) => ({ ...style, updatedAt: "2026-09-25T00:00:00Z" }));
    const user = userEvent.setup();

    render(<SourceLabelSettingsPanel />);
    expect(await screen.findByText("DRIVER SPHERE")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Anton font" }));
    await user.clear(screen.getByLabelText("Text color hex value"));
    await user.type(screen.getByLabelText("Text color hex value"), "#12abef");
    await user.click(screen.getByRole("button", { name: "Save source label" }));

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        expect.objectContaining({ fontPreset: "anton", fillColor: "#12ABEF" }),
      ),
    );
    expect(screen.getByText(/saved for new jobs/i)).toBeInTheDocument();
  });

  it("updates the live preview and resets every control to the reference style", async () => {
    vi.spyOn(api, "sourceLabelSettings").mockResolvedValue(defaults);
    const save = vi
      .spyOn(api, "saveSourceLabelSettings")
      .mockImplementation(async (style) => ({ ...style, updatedAt: "2026-09-25T00:00:00Z" }));
    const user = userEvent.setup();
    render(<SourceLabelSettingsPanel />);

    const label = await screen.findByText("DRIVER SPHERE");
    await user.click(screen.getByRole("button", { name: "Roboto Condensed font" }));
    fireEvent.change(screen.getByLabelText("Responsive size slider"), {
      target: { value: "6.25" },
    });
    await user.click(screen.getByRole("button", { name: "Use #000000 for text color" }));

    expect(label).toHaveStyle({ color: "rgb(0, 0, 0)" });
    expect(label.style.fontFamily).toContain("Source Roboto Condensed");
    expect(screen.getByLabelText("Responsive size percentage")).toHaveValue(6.25);

    await user.click(screen.getByRole("button", { name: "Reset defaults" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(DEFAULT_STYLE));
    expect(screen.getByRole("button", { name: "Bebas Neue font" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByLabelText("Responsive size percentage")).toHaveValue(4);
  });

  it("blocks saving an invalid hex color", async () => {
    vi.spyOn(api, "sourceLabelSettings").mockResolvedValue(defaults);
    const user = userEvent.setup();
    render(<SourceLabelSettingsPanel />);

    const input = await screen.findByLabelText("Outline color hex value");
    await user.clear(input);
    await user.type(input, "black");

    expect(screen.getByText(/six-digit color/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save source label" })).toBeDisabled();
  });
});

const DEFAULT_STYLE = {
  fontPreset: "bebas-neue" as const,
  fillColor: "#FFFFFF",
  outlineColor: "#000000",
  sizePercent: 4,
};

