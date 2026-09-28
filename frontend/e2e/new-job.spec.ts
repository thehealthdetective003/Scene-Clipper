import { expect, test } from "@playwright/test";

test("keeps numbered link and file sources paired with editable names", async ({ page }) => {
  await page.goto("/new");

  const sourceOne = page.getByTestId("source-row-1");
  await expect(sourceOne.getByRole("heading", { name: /Source 1.*Video link/ })).toBeVisible();
  await sourceOne.getByLabel("Video link for source 1").fill("https://example.com/first-video");
  await sourceOne.getByLabel(/Source name for source 1/).fill("First publisher");

  await page.getByRole("button", { name: "Add another video link" }).click();
  const sourceTwo = page.getByTestId("source-row-2");
  await sourceTwo.getByLabel("Video link for source 2").fill("https://example.com/second-video");
  await sourceTwo.getByLabel(/Source name for source 2/).fill("第二来源");

  await page.getByRole("button", { name: "Add a video file" }).click();
  const sourceThree = page.getByTestId("source-row-3");
  await expect(sourceThree.getByRole("heading", { name: /Source 3.*Video file/ })).toBeVisible();
  await sourceThree.getByLabel(/Source name for source 3/).fill("Uploaded source");

  await expect(sourceOne.getByLabel(/Source name for source 1/)).toHaveValue("First publisher");
  await expect(sourceTwo.getByLabel(/Source name for source 2/)).toHaveValue("第二来源");
  await expect(sourceThree.getByLabel(/Source name for source 3/)).toHaveValue("Uploaded source");
  await expect(page.getByRole("button", { name: "Analyse 3 sources" })).toBeDisabled();
  await expect(page.getByText("0 of 3 ready. This numbered order stays fixed while transfers finish.")).toBeVisible();
});
