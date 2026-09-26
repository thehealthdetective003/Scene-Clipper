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
upload → probe → detect boundaries → safe intervals → local features
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

Generate the two secrets and put them in `.env`. These run before the stack
exists, so they do not go through Compose:

```bash
# APP_ENCRYPTION_KEY — base64 of 32 random bytes
openssl rand -base64 32

# ADMIN_PASSWORD_HASH — Argon2id. The password is typed at a prompt, never
# passed as an argument, so it never lands in shell history.
docker build -t scene-clipper-backend ./backend
docker run --rm -it scene-clipper-backend python -m app.cli hash-password
```

**Escape the hash for Compose.** An Argon2id PHC string is full of `$`
(`$argon2id$v=19$m=65536,...`), and Docker Compose treats `$` in `.env` as a
variable reference — it will silently eat `$argon2id`, `$v`, and `$m`, and the
API will then refuse to start with "must be an Argon2id PHC string". Double
every `$` when writing the value into `.env`:

```bash
# $argon2id$v=19$...  ->  $$argon2id$$v=19$$...
python - <<'PY'
import pathlib
p = pathlib.Path(".env"); out = []
for line in p.read_text().splitlines():
    if line.startswith("ADMIN_PASSWORD_HASH="):
        k, _, v = line.partition("=")
        line = f"{k}={v.replace('$$', '$').replace('$', '$$')}"   # idempotent
    out.append(line)
p.write_text("\n".join(out) + "\n")
PY
```

The doubled form is what belongs in `.env`; Compose passes the single-`$`
value through to the container. `APP_ENCRYPTION_KEY` is base64 and needs no
escaping.

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

1. **Sign in** with the shared administrator credentials.
2. **Settings** → choose the source-label font, text/outline colors, and
   responsive size. New jobs snapshot these defaults. You can also add a
   Gemini API key (optional); it is validated, encrypted with AES-256-GCM, and
   never displayed again — not even partially.
3. **New job** → drop in one MP4/MOV/MKV/WebM. The upload is resumable: an
   interrupted transfer continues from the last verified byte.
4. Choose a target clip count (1–100), optional source name, and optional focus
   prompt. A source name is normalized to one uppercase Latin-script line and
   appears at the top left of review previews and every exported MP4. Leave it
   blank for unchanged, unlabelled clips.
5. Watch progress live. Closing the tab does not affect the job.
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
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD_HASH=$(echo 'a-long-dev-password' | python -m app.cli hash-password)
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
python -m app.cli hash-password    # new ADMIN_PASSWORD_HASH
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
cd frontend && E2E_PASSWORD=... npx playwright test
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
* Out of scope by design: URL/platform imports, multiple sources per job,
  automatic social crops, reframing, captions, and transcription.
