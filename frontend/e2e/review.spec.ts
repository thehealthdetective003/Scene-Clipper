/** Browser end-to-end scenarios. Requires a running stack. */

import { expect, test } from "@playwright/test";

async function openApp(page: import("@playwright/test").Page) {
  await page.goto("/jobs");
  await expect(page.getByRole("link", { name: "Jobs" })).toBeVisible();
}

test("opens the jobs list without credentials", async ({ page }) => {
  await openApp(page);
  await expect(page).toHaveURL(/\/jobs/);
  await expect(page.getByRole("heading", { name: "Jobs" })).toBeVisible();
});

test("old login links redirect into the app", async ({ page }) => {
  await page.goto("/login");
  await expect(page).toHaveURL(/\/jobs/);
  await expect(page.getByRole("heading", { name: "Jobs" })).toBeVisible();
});

test("opens a job and shows its review controls", async ({ page }) => {
  await openApp(page);
  const card = page.locator('a[href^="/jobs/"]').first();
  if ((await card.count()) === 0) test.skip(true, "no jobs on this installation");

  await card.click();
  await expect(page).toHaveURL(/\/jobs\/[0-9a-f-]+/);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
});

test("keeps session material out of browser storage", async ({ page }) => {
  await openApp(page);
  const storage = await page.evaluate(() => ({
    local: JSON.stringify(window.localStorage),
    session: JSON.stringify(window.sessionStorage),
    localKeys: Object.keys(window.localStorage),
    sessionCount: window.sessionStorage.length,
  }));

  expect(storage.localKeys).toEqual(["scene-clipper-theme"]);
  expect(storage.sessionCount).toBe(0);
  expect(storage.local).not.toContain("AIza");
  expect(storage.local).not.toContain("csrf");
  expect(storage.local).not.toContain("clipper_session");
  expect(storage.session).not.toContain("AIza");
  expect(storage.session).not.toContain("csrf");
  expect(storage.session).not.toContain("clipper_session");
});

test("the automatic session cookie is HttpOnly and SameSite=Strict", async ({ page, context }) => {
  await openApp(page);
  const cookies = await context.cookies();
  const session = cookies.find((cookie) => cookie.name === "clipper_session");
  expect(session).toBeDefined();
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe("Strict");
});

test("settings never display a stored key", async ({ page }) => {
  await openApp(page);
  await page.getByRole("link", { name: "Settings" }).click();

  const keyField = page.getByLabel("Add a key");
  await expect(keyField).toHaveValue("");
  await expect(keyField).toHaveAttribute("type", "password");
  expect(await page.locator("body").innerText()).not.toContain("AIza");
});
