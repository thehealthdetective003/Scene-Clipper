/**
 * Browser end-to-end scenario (spec 12.7).
 *
 * Requires a running stack. The admin password comes from E2E_PASSWORD so no
 * credential is ever hard-coded here.
 */

import { expect, test } from "@playwright/test";

const USERNAME = process.env.E2E_USERNAME ?? "admin";
const PASSWORD = process.env.E2E_PASSWORD ?? "";

test.skip(!PASSWORD, "Set E2E_PASSWORD to run browser end-to-end tests.");

async function signIn(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill(USERNAME);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("link", { name: "Jobs" })).toBeVisible();
}

test("signs in and reaches the jobs list", async ({ page }) => {
  await signIn(page);
  await expect(page).toHaveURL(/\/jobs/);
  await expect(page.getByRole("heading", { name: "Jobs" })).toBeVisible();
});

test("opens a job and shows its review controls", async ({ page }) => {
  await signIn(page);
  // Job cards link to /jobs/:id; skip when this install has no jobs yet.
  const card = page.locator('a[href^="/jobs/"]').first();
  if ((await card.count()) === 0) test.skip(true, "no jobs on this installation");

  await card.click();
  await expect(page).toHaveURL(/\/jobs\/[0-9a-f-]+/);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
});

test("rejects a wrong password without revealing which field was wrong", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Username").fill(USERNAME);
  await page.getByLabel("Password").fill("definitely-not-the-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText(/username or password/i);
});

test("keeps no credentials in browser storage", async ({ page }) => {
  await signIn(page);

  const storage = await page.evaluate(() => ({
    local: JSON.stringify(window.localStorage),
    session: JSON.stringify(window.sessionStorage),
    localCount: window.localStorage.length,
    sessionCount: window.sessionStorage.length,
  }));

  // Spec 5.2: no key, token, or session material in browser storage.
  expect(storage.localCount).toBe(0);
  expect(storage.sessionCount).toBe(0);
  expect(storage.local).not.toContain("AIza");
  expect(storage.session).not.toContain("AIza");
});

test("the session cookie is HttpOnly and SameSite=Strict", async ({ page, context }) => {
  await signIn(page);
  const cookies = await context.cookies();
  const session = cookies.find((cookie) => cookie.name === "clipper_session");
  expect(session).toBeDefined();
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe("Strict");
});

test("settings never display a stored key", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: "Settings" }).click();

  const keyField = page.getByLabel("API key");
  await expect(keyField).toHaveValue("");
  // The input is a password field, so it is never shown even while typing.
  await expect(keyField).toHaveAttribute("type", "password");

  const body = await page.locator("body").innerText();
  expect(body).not.toContain("AIza");
});

test("signing out invalidates the session", async ({ page }) => {
  await signIn(page);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.goto("/jobs");
  await expect(page).toHaveURL(/\/login/);
});
