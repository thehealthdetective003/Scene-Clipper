# Long-Form Video Scene Clipper

Turn one long video into a reviewed, serially numbered collection of the best
1–5 second continuous shots — without ever crossing a cut, fade, or dissolve.

This is a **private, self-hosted** application with a single shared
administrator login. It is not a public SaaS product.

Implemented from [`long-form-video-scene-clipper-spec.md`](./long-form-video-scene-clipper-spec.md).

---

## How it works

Shot boundaries are found **locally** — no LLM participates in that decision.
FFmpeg and PySceneDetect determine every timecode; Gemini is used only to rank
shots that already passed the continuity gate, to explain the ranking, and to
pick the best maximum-length region inside a long shot.

```
upload or link → download/probe → detect boundaries → safe intervals → local features
                                                          ↓
                       review ← rank (Gemini optional) ← contact sheets
                          ↓
                       export → validate → ZIP
```

Guarantees the pipeline enforces:

| Invariant | Where it is enforced |
|---|---|
| A clip never leaves one detected shot | `analysis/intervals.py`, re-checked on every review edit |
| A clip is 1.0–5.0s within one source frame | `MIN_CLIP_US`/`MAX_CLIP_US` in `media/timebase.py`; enforced by `services/review.py` |
| One clip maximum per detected shot | one `CandidateShot` per `DetectedShot`, DB-unique |
| Keys are never exposed after submission | `security/crypto.py`, `logging_setup.py` |
| Gemini failure never discards local analysis | `analysis/ranking.py` |

Motion, handheld shake, blur, and fast subjects **lower a score but never
disqualify a shot**. Only cuts, fades, dissolves, and corruption do.

---

## Requirements

* Docker and Docker Compose (the only requirement for a normal deployment)
* For local development: Python 3.11, Node 22, FFmpeg 5.1+, Redis

---

## Deploy

```bash
cp .env.example .env
```

Generate the encryption key and put it in `.env`:

```bash
# APP_ENCRYPTION_KEY — base64 of 32 random bytes
openssl rand -base64 32
```

Set `APP_BASE_URL` and `APP_HOSTNAME` to the hostname you will actually use,
then start everything:

```bash
docker compose up -d --build
```

The application refuses to start if the encryption key, administrator username,
or password hash is missing or malformed — a misconfigured deployment fails
immediately rather than serving in an unsafe state.

### TLS

Caddy provisions a certificate automatically for a public hostname. For an
internal hostname, either use Caddy's local CA or supply your own certificate —
edit the site block in [`Caddyfile`](./Caddyfile):

```caddyfile
{$APP_HOSTNAME} {
    tls internal                          # Caddy's local CA
    # tls /certs/site.pem /certs/site.key # or your own certificate
    ...
}
```

HSTS is only sent once TLS is actually terminating, and session cookies are
marked `Secure` for every non-loopback deployment.

---

## Use

1. Open Scene Clipper; a local browser session starts automatically.
2. **Settings** → choose the source-label font, text/outline colors, and
   responsive size. New jobs snapshot these defaults. You can also add a
   Gemini API key (optional); it is validated, encrypted with AES-256-GCM, and
   never displayed again — not even partially.
3. **New job** → build up to 12 numbered source rows containing public video
   links, MP4/MOV/MKV/WebM files, or a mix of both. Each row accepts its source
   name before preparation and keeps that name editable afterward. Add another
   link or file with the controls below the list. Transfers run concurrently,
   but completion speed never changes the visible or submitted source order.
   Link imports use the highest available quality and automatically suggest the
   video's channel name as an editable source name. File uploads are resumable.
   Playlists, channels, private videos, and live/upcoming streams are not imported.
4. A source name is normalized to one uppercase line and appears at the top
   left of that source's review previews and exported MP4s. Latin, Chinese,
   Japanese, and Korean names are supported. Each source also has an independent
   ranking instruction. Automatic ranking and preselection are optional; leave
   them off to start with a blank manual selection.
5. Watch progress live. The analysis dashboard shows the real task and
   percentage for every source independently: metadata reading, frame scanning,
   candidate measurement, ranking/manual preparation, and preview rendering.
   It also reports detected and usable-scene counts as they become available.
   Closing the tab does not affect the job, and reopening it restores the
   durable progress snapshot.
6. **Review**: preview clips, deselect, reorder by drag, and adjust trims. The
   handles cannot leave the safe interval, and the server revalidates and
   frame-quantizes every trim anyway.
7. **Export** at original / max-1080p / max-720p, with or without audio, and
   download a ZIP containing the clips plus JSON and CSV manifests.

Fewer usable shots than you asked for is a **successful** result: the job
reports the requested count, the delivered count, and why the rest were
excluded.

### What is sent to Gemini

Representative **still frames** assembled into contact sheets, and — rarely,
capped at five candidates per job — a short, silent, low-resolution excerpt to
resolve ambiguous motion. **Your full source video is never sent.** With no key
configured, or a cap of `0`, nothing leaves the deployment at all.

---

## Local development

```bash
# Backend
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
export APP_ENCRYPTION_KEY=$(python -m app.cli generate-key)
export DATA_DIR=$PWD/.devdata APP_BASE_URL=http://localhost:5173 HTTPS_ENABLED=false
python -m alembic upgrade head
uvicorn app.main:app --reload --port 8000

# Worker (second terminal, same environment)
python -m app.workers.main

# Frontend (third terminal)
cd frontend && npm install && npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api` to port 8000 so the browser
still sees a single origin and cookies behave exactly as in production. Loopback
origins are the one case where an API key may be submitted over plain HTTP.

If FFmpeg is not on `PATH`, point at it explicitly:

```bash
export FFMPEG_PATH=/path/to/ffmpeg FFPROBE_PATH=/path/to/ffprobe
```

### Operator commands

```bash
python -m app.cli generate-key     # new APP_ENCRYPTION_KEY
python -m app.cli check-config     # validate the environment
python -m app.cli check-media      # verify ffmpeg/ffprobe are usable
python -m app.cli requeue          # requeue abandoned work after a crash
```

---

## Tests

Gemini is mocked for every routine test; the detection matrix runs the real
local pipeline against synthetic fixtures with frame-accurate transition
annotations.

```bash
cd backend  && .venv/bin/python -m pytest        # 400+ tests
cd frontend && npm run typecheck && npx vitest run
```

Media-dependent tests skip automatically when FFmpeg is unavailable.

Two suites are opt-in:

```bash
# Real Gemini API — tiny fixture, request cap of 1
GEMINI_SMOKE_KEY=... .venv/bin/python -m pytest -m credentialed

# Browser end-to-end — needs a running stack
cd frontend && npx playwright test
```

---

## Layout

```
backend/
  app/
    analysis/    detection, safe intervals, features, ranking, budget, cache
    api/         routes, error envelope, serializers, streaming
    exporting/   clip encoding, manifests, ZIP packaging
    media/       timebase math, ffprobe, FFmpeg, sandboxed subprocess runner
    providers/   Gemini adapter behind a provider-agnostic interface
    security/    AES-256-GCM, Argon2id, sessions, CSRF, origin, rate limits
    services/    uploads, jobs, review, exports, events, storage, idempotency
    workers/     RQ tasks, leases, recovery sweep
  alembic/       migrations
  tests/         unit, media matrix, API, security, recovery
frontend/                 React 18 · TypeScript · Vite · Tailwind v4 · Framer Motion
  src/api/       typed client; CSRF and keys stay in memory only
  src/components/ui/  design-system primitives (spotlight cards, glow borders,
                      segmented control, switch, progress, badges)
  src/lib/       resumable uploader, formatting, `cn` class helper
  src/pages/     login, jobs, new job, review
  src/styles.css design tokens (@theme) -- dark violet palette, radii, keyframes
  tests/ e2e/    Vitest unit tests, Playwright browser tests
```

---

## Operational notes

* **Storage.** Everything lives under `DATA_DIR` (`/data` in Docker). Source
  media and exports are never served statically — every byte goes through an
  authenticated, range-capable route. Files are published by atomic rename only
  after validation, so a failed export leaves no partial archive.
* **Retention.** Nothing is deleted automatically. `RETENTION_ENABLED` defaults
  to `false`; sources and exports persist until you delete the job.
* **Deletion.** Deleting a job tombstones it immediately and hides it from the
  API; the worker then removes previews, sheets, proxies, attempts, and exports,
  and drops the source once its last reference is gone.
* **Crash safety.** Stages commit checkpoints before they are acknowledged and
  workers hold heartbeated leases, so a killed worker resumes from the last
  checkpoint without duplicating candidates.
* **Request budget.** Each job snapshots its cap. A unit is reserved durably
  before transmission; if a worker dies mid-request the unit stays consumed,
  which keeps the cap honest across crashes.
* **Backups.** Stop the stack (or checkpoint the WAL) and copy the `app-data`
  volume. **`APP_ENCRYPTION_KEY` is not in that volume** — back it up
  separately, or the stored Gemini key becomes permanently unreadable.

---

## Known limitations

* A very low-contrast, slow cross-dissolve between two similarly lit shots can
  fall below the spec's absolute content-score floor of 8 and go undetected.
  `tests/fixtures_media.py::low_contrast_dissolve` documents this boundary.
* Subprocess CPU/memory rlimits are POSIX-only. On Windows, media subprocesses
  are bounded by wall-clock timeout alone; the Docker deployment gets the full
  set.
* Out of scope by design: private/authenticated video imports, playlists,
  automatic social crops, reframing, captions, and transcription.
