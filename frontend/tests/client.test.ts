/**
 * Client-side secret hygiene (spec 5.2, 10.4): the CSRF token and the Gemini
 * key must never reach localStorage, sessionStorage, IndexedDB, or a URL.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, getCsrfToken, setCsrfToken } from "../src/api/client";

const SENTINEL_KEY = "AIzaSyFAKEKEYSENTINEL_do_not_log_0000000";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    setCsrfToken(null);
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps the CSRF token in memory only", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ authenticated: true, csrfToken: "csrf-secret-value" }),
    );

    await api.session();

    expect(getCsrfToken()).toBe("csrf-secret-value");
    expect(JSON.stringify(localStorage)).not.toContain("csrf-secret-value");
    expect(JSON.stringify(sessionStorage)).not.toContain("csrf-secret-value");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("sends the CSRF header on state-changing requests only", async () => {
    setCsrfToken("csrf-secret-value");
    fetchMock.mockResolvedValue(jsonResponse({ items: [], nextCursor: null }));

    await api.listJobs();
    const getHeaders = (fetchMock.mock.calls[0]?.[1] as RequestInit).headers as Record<string, string>;
    expect(getHeaders["X-CSRF-Token"]).toBeUndefined();

    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await api.deleteAllGeminiKeys();
    const deleteHeaders = (fetchMock.mock.calls[1]?.[1] as RequestInit).headers as Record<string, string>;
    expect(deleteHeaders["X-CSRF-Token"]).toBe("csrf-secret-value");
  });

  it("never puts the Gemini key in a URL and never stores it", async () => {
    setCsrfToken("csrf");
    fetchMock.mockResolvedValue(
      jsonResponse({ configured: true, model: "m", requestCap: 8, updatedAt: null }),
    );

    await api.addGeminiKey(SENTINEL_KEY, "gemini-test-model", 8);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).not.toContain(SENTINEL_KEY);
    expect(url).toBe("/api/v1/settings/gemini");
    // It travels in the body, exactly once.
    expect(String(init.body)).toContain(SENTINEL_KEY);

    expect(JSON.stringify(localStorage)).not.toContain(SENTINEL_KEY);
    expect(JSON.stringify(sessionStorage)).not.toContain(SENTINEL_KEY);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("sends same-origin credentials", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], nextCursor: null }));
    await api.listJobs();
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.credentials).toBe("same-origin");
  });

  it("clears the CSRF token on a 401", async () => {
    setCsrfToken("csrf-secret-value");
    fetchMock.mockResolvedValue(
      jsonResponse(
        { error: { code: "unauthorized", message: "Sign in", retryable: false, requestId: "r", details: {} } },
        401,
      ),
    );

    await expect(api.listJobs()).rejects.toThrow(ApiError);
    expect(getCsrfToken()).toBeNull();
  });

  it("surfaces the error envelope fields", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "stale_review_revision",
            message: "Reload and try again.",
            retryable: false,
            requestId: "req-1",
            details: { currentRevision: 4 },
          },
        },
        409,
      ),
    );

    await expect(api.candidates("job-1")).rejects.toMatchObject({
      code: "stale_review_revision",
      status: 409,
      details: { currentRevision: 4 },
    });
  });

  it("attaches an Idempotency-Key when one is supplied", async () => {
    setCsrfToken("csrf");
    fetchMock.mockResolvedValue(jsonResponse({ id: "job-1" }));
    await api.createJob("upload-1", 20, null, "Driver Sphere", true, "key-123");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBe("key-123");
    expect(JSON.parse(String(init.body))).toMatchObject({ sourceName: "Driver Sphere" });
  });

  it("saves source-label typography through its dedicated settings route", async () => {
    setCsrfToken("csrf");
    const style = {
      fontPreset: "bebas-neue" as const,
      fillColor: "#FFFFFF",
      outlineColor: "#000000",
      sizePercent: 4,
    };
    fetchMock.mockResolvedValue(jsonResponse({ ...style, updatedAt: null }));

    await api.saveSourceLabelSettings(style);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/settings/source-label");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual(style);
  });
});
