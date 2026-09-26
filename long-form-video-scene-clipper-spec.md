# Long-Form Video Scene Clipper

Implementation-ready specification for a private, self-hosted web application.

**Document status:** Draft  
**Primary audience:** Coding agents and engineers  
**MVP deployment:** One private installation with one shared administrator login  
**Primary objective:** Turn one long-form source video into a reviewed, serially numbered collection of the best 3–6 second continuous shots while minimizing Gemini API use.

---

## 1. Instructions to the Implementer

Build the application described here as a production-minded MVP. The words **MUST**, **SHOULD**, and **MAY** indicate required, recommended, and optional behavior.

Do not substitute an LLM or video-language model for exact shot-boundary detection. Shot boundaries and export timecodes MUST be determined locally. Gemini is used only to rank eligible shots, explain the ranking, and choose the best six-second region inside long shots.

When this document and an implementation detail conflict, preserve these invariants:

1. An exported clip MUST remain entirely inside one detected continuous shot.
2. An exported clip MUST be between 3.0 and 6.0 seconds, inclusive, within one source-frame tolerance.
3. Each detected shot can contribute at most one exported clip.
4. Gemini keys MUST never be exposed after submission or written in plaintext.
5. A Gemini failure or request-cap exhaustion MUST not discard completed local analysis.

---

## 2. Product Definition

### 2.1 Problem

Manually finding reusable shots in a long video is slow. The application should detect editorial shot boundaries, find the strongest usable shots, let the user review them, and export clean, short clips without crossing a cut, fade, or dissolve.

### 2.2 Target user

The MVP is for an individual editor or a small trusted team operating a private, self-hosted installation. It is not a public SaaS product.

### 2.3 Core outcome

Given one uploaded video and an optional focus prompt, the application produces up to the requested number of ranked clips. Each clip:

- contains one uninterrupted camera shot;
- is between 3 and 6 seconds long;
- uses the best six-second region when its source shot is longer than six seconds;
- can retain synchronized source audio or be muted;
- can be exported at original size, a 1080p maximum, and/or a 720p maximum;
- is stored with a gapless serial name such as `0001.mp4`.

### 2.4 Definition of “smooth shot”

For this product, “smooth” means **editorially continuous**, not physically stable. A shot remains eligible if it contains intentional or accidental motion, including:

- pans, tilts, zooms, or camera moves;
- handheld shake;
- fast-moving subjects;
- motion blur or soft focus.

These properties MAY lower a ranking score but MUST NOT be hard rejection rules. Cuts, fades, dissolves, corrupted frames, and gaps that join two camera angles are disqualifying boundaries.

### 2.5 In scope

- One local video upload per job.
- MP4, MOV, MKV, and WebM containers.
- Resumable uploads and restart-safe background processing.
- Local metadata probing and shot-boundary detection.
- Optional Gemini-assisted ranking using a user-supplied key.
- Optional natural-language focus prompt.
- Review, deselection, reordering, and constrained trimming.
- Original, maximum 1080p, and maximum 720p exports.
- Optional source audio, enabled by default.
- ZIP download containing clips and JSON/CSV manifests.
- Manual job and file deletion.

### 2.6 Out of scope for the MVP

- YouTube, social-network, or arbitrary URL importing.
- Multiple source videos in one job.
- Public registration, individual user accounts, billing, or subscriptions.
- Automatic 9:16, 1:1, or 4:5 cropping.
- Subject-aware reframing.
- Captions, transcription, translation, music, or generative editing.
- Mobile applications.
- Automatic retention deletion; retention settings may exist but default to disabled.

---

## 3. Terminology

| Term | Meaning |
|---|---|
| **Upload** | The resumably transferred source file before or during verification. |
| **Job** | One complete analysis and review workflow for one source video. |
| **Detected shot** | A time range between local transition boundaries. |
| **Safe interval** | The detected shot after transition regions and boundary guards are removed. |
| **Candidate** | A detected shot whose safe interval is at least 3 seconds. |
| **Recommended clip** | The 3–6 second interval proposed for one candidate. |
| **Selected clip** | A candidate included in the current user-reviewed export order. |
| **Contact sheet** | A locally generated image containing labeled, timestamped frames from one or more candidates. |
| **Proxy video** | A short, low-resolution candidate excerpt sent to Gemini only as a capped fallback. |

All media positions in APIs and persistence MUST use integer microseconds. Frame boundaries MUST be calculated using the source time base rather than floating-point seconds.

---

## 4. Primary User Flow

1. The user signs in with the shared administrator credentials.
2. The user opens Settings and adds or replaces a Gemini API key.
3. The backend validates the key and saves only encrypted key material.
4. The user creates an upload by dragging or selecting one supported video.
5. The browser uploads sequential chunks and can resume from the last verified byte.
6. After verification, the user configures:
   - target clip count, default `20`, allowed range `1–100`;
   - optional per-video source name, maximum 48 characters;
   - optional focus prompt, maximum 2,000 characters;
   - Gemini request cap, inherited from the administrator default;
   - whether analysis may use the configured Gemini key.
7. The job progresses through probing, detection, local preparation, and ranking.
8. The review page displays the automatically selected top candidates and any fallback or partial-result warning.
9. The user previews clips, removes clips, changes their order, or adjusts trims within the safe interval.
10. The user selects one or more output resolutions and chooses whether to keep audio.
11. The application exports, validates, packages, and provides the ZIP download.
12. The source and generated files remain available until the user explicitly deletes the job.

---

## 5. Functional Requirements

### 5.1 Authentication

- The MVP MUST have one shared administrator account.
- The deployment MUST be initialized with an Argon2id password hash, not a plaintext password.
- Login MUST create a server-side session and rotate the session identifier.
- Logout MUST invalidate the session immediately.
- Session cookies MUST be `HttpOnly`, `SameSite=Strict`, and `Secure` whenever HTTPS is enabled.
- Login MUST enforce exact same-origin `Origin`/`Referer` and Fetch Metadata validation plus rate limiting, but does not require a session CSRF token.
- Every authenticated state-changing request MUST require a CSRF token and exact same-origin validation.
- All upload, preview, job, export, download, and settings routes MUST require authentication.

### 5.2 Gemini key management

- The settings page MUST provide test, save/replace, and delete actions.
- The request cap MUST be an integer from 0–50. A value of `0` forces local-only analysis; the default is `8`.
- The browser MUST send the key only over an authenticated HTTPS request, except that `http://localhost` and loopback IP origins are permitted for same-machine development/self-hosting.
- The frontend MUST NOT place the key in local storage, session storage, IndexedDB, URLs, analytics, logs, or error telemetry.
- The backend MUST encrypt the key with AES-256-GCM using:
  - a 32-byte deployment master key supplied through the environment;
  - a new random 96-bit nonce for every write;
  - authenticated metadata containing the settings-record identifier and encryption-format version.
- Persist only the encryption version, nonce, ciphertext, and authentication tag.
- The master key MUST NOT be stored in SQLite or committed to source control.
- The stored Gemini key MUST never be returned through the API, including masked or partially revealed forms.
- The backend decrypts the key only immediately before an outbound Gemini call. It MUST NOT place the plaintext key in queue payloads.
- Key endpoints and authorization headers MUST be excluded from request-body logging and tracing.

Google explicitly recommends keeping Gemini keys out of client-side production code and using a backend proxy: [Gemini API key security](https://ai.google.dev/gemini-api/docs/api-key).

### 5.3 Resumable video upload

- One upload corresponds to one source file.
- Default maximum file size is 20 GiB and MUST be configurable.
- The default chunk size is 16 MiB and MUST be reported by the create-upload response.
- Chunks are sequential and idempotent by upload ID, byte offset, length, and checksum.
- An identical retransmission is accepted; overlapping content with different bytes is rejected.
- The server MUST validate the declared length before transfer and enforce it while streaming.
- Original filenames are display-only metadata. Server-generated UUID paths MUST be used on disk.
- On completion, the backend computes the authoritative SHA-256 and validates the media with `ffprobe`.
- Extension and browser-provided MIME type are hints only. A codec is supported when the pinned FFmpeg build can fully probe it and decode a bounded smoke-test interval within configured resource limits.
- The job MUST reject a corrupt file, a file with no video stream, a failed decode smoke test, an unsupported container/codec, or a duration-less stream.
- Upload verification is asynchronous and restart-safe.

Chunk retry semantics:

- The normal request offset equals the server’s current verified offset.
- An offset greater than the current offset returns `409 upload_offset_mismatch` with `details.expectedOffset`.
- An offset lower than the current offset is accepted only when its range and checksum exactly match already verified bytes; it then returns the unchanged current offset.
- A mismatched retransmission returns `409 upload_chunk_mismatch`.
- Success returns `204 No Content` with the new current `Upload-Offset` header.

### 5.4 Job configuration

Each job snapshots the following values so later setting changes do not alter an in-progress run:

- upload ID and source SHA-256;
- target clip count;
- normalized optional focus prompt;
- normalized optional source name and its current global typography style;
- Gemini model identifier;
- Gemini request cap;
- detector configuration version;
- analysis-pipeline version.

The default target is 20 clips. Valid targets are 1–100.

The optional source name is normalized to one uppercase line with a maximum of
48 characters. Latin letters and combining marks, numbers, spaces, and
punctuation are accepted; control characters and non-Latin scripts are
rejected. A blank name stores no style snapshot and leaves previews and exports
unlabelled. Global source-label settings provide four bundled presets (Bebas
Neue, Anton, Oswald SemiBold, and Roboto Condensed Bold), opaque six-digit fill
and outline colors, and a responsive size from 2.5% to 8% in 0.25% increments.
Changing those defaults affects only subsequently created jobs.

`useGemini` defaults to true when a key is configured and false otherwise. If it is true but no usable key remains when ranking begins, the job completes with local fallback and a warning instead of losing its local work.

### 5.5 Progress, cancellation, retry, and recovery

The user-visible state machine is:

```text
uploaded → probing → detecting → ranking → review-ready → exporting → complete
                         ↘ failed
                         ↘ cancelled
complete → exporting → complete  (later export)
```

- Probing, detecting, and ranking MAY transition to `failed` or `cancelled`.
- Export work has its own state. A failed or cancelled export returns the job to its previous stable state: `review-ready` for the first export or `complete` for a later export.
- Progress percentage MUST be monotonic within a stage.
- The application MUST persist stage checkpoints before acknowledging completion.
- A disconnected client can reconnect without affecting the worker.
- Cancellation MUST stop new work, terminate the active media subprocess, remove attempt-only temporary files, and retain the verified source and completed checkpoints.
- Retrying a failed job MUST resume at the last valid checkpoint instead of repeating completed work.
- Workers MUST use leases/heartbeats. On startup, abandoned nonterminal jobs are requeued from the last incomplete stage.
- Each stage and export attempt MUST be idempotent. Publish files atomically only after validation.

### 5.6 Review experience

When ranking finishes, show:

- ranked position and current export order;
- thumbnail and authenticated preview;
- the snapshotted source label in the preview when configured;
- source start/end timecodes and recommended duration;
- overall score and confidence;
- short ranking reason;
- scoring source (`contact-sheet`, `proxy-video`, or `local-fallback`) and cache status (`none`, `partial`, or `complete`);
- partial-result and API-budget warnings.

Automatically select up to the target count. If fewer eligible candidates exist, select all of them and show the requested count, delivered count, and exclusion summary. This is a successful result, not an error.

The user can:

- preview any candidate;
- select or deselect a candidate;
- reorder selected clips;
- adjust start/end handles;
- restore the recommended trim.

Review updates are allowed while the job is `review-ready` or `complete`, provided no export is active. Updating a completed job creates a new review revision but does not invalidate prior exports.

The server MUST reject a reviewed trim that:

- is shorter than 3.0 seconds or longer than 6.0 seconds;
- falls outside the candidate safe interval;
- references a candidate from another job;
- selects the same candidate more than once;
- produces non-gapless order values.

The client MUST use an optimistic review revision. A stale update receives `409 Conflict` and the latest review state.

### 5.7 Export

- Export uses H.264 video and AAC audio in an MP4 container.
- Cuts MUST be frame-accurate. Do not use keyframe-limited stream copying.
- Default video encoding is `libx264`, CRF 20, `medium` preset, `yuv420p`, and MP4 fast-start.
- Default audio encoding is AAC at 192 kbps, using the default audio stream or otherwise the first audio stream.
- Apply timestamp-based video/audio trims and reset both output timelines to zero to maintain synchronization.
- Preserve the source display aspect ratio and decoded-frame cadence. Apply rotation to the encoded pixels, clear rotation metadata, and normalize sample aspect ratio to 1:1.
- Audio is included and synchronized by default. Muted exports contain no audio stream.
- A silent source MUST export successfully even when audio is requested.
- In an audio-enabled output, both streams MUST begin at zero within one stream time-base tick, and their end times MUST differ by no more than the greater of one video-frame duration or one encoded audio-packet duration.
- Use `yuv420p` for broad playback compatibility unless preserving the source requires a documented compatible alternative.
- Add MP4 fast-start metadata.
- Validate every output with `ffprobe` and a decode pass before publishing it.
- For labelled jobs, apply the snapshotted label after scaling in both review
  previews and exports. Position it 0.75% from the left and 1% from the top,
  size it relative to frame height, and reduce the font size only when needed
  to keep the complete label on one line. Pass text to FFmpeg through a
  temporary UTF-8 text file rather than interpolating it into the filtergraph.

Resolution rules:

| Option | Rule |
|---|---|
| `original` | Use the source display dimensions after applying orientation metadata. |
| `max1080p` | Fit within 1920×1080 while preserving aspect ratio. |
| `max720p` | Fit within 1280×720 while preserving aspect ratio. |

No preset may exceed either source dimension. Scaling MUST preserve aspect ratio and produce encoder-compatible even dimensions. If two requested presets resolve to the same dimensions, both requested folders may be produced for predictable packaging, but the implementation SHOULD reuse the validated encode internally.

The ZIP layout is:

```text
scene-clips-{jobId}-{exportId}.zip
├── original/
│   ├── 0001.mp4
│   ├── 0002.mp4
│   └── ...
├── 1080p/
│   ├── 0001.mp4
│   └── ...
├── 720p/
│   ├── 0001.mp4
│   └── ...
├── manifest.json
└── manifest.csv
```

Only requested resolution folders are included. Default order is ascending source time. User-defined review order overrides it. Files are renumbered at export so every included folder begins at `0001.mp4` and has no gaps.

---

## 6. Video Analysis Pipeline

### 6.1 Stage A: probe and normalize metadata

Use `ffprobe` to collect:

- duration and time base;
- coded and display dimensions;
- rotation/orientation;
- average and real frame-rate rationals;
- variable-frame-rate indicators;
- video and audio codecs;
- audio presence, channels, and sample rate;
- color and HDR metadata when present.

Do not transcode the full source as an analysis prerequisite. Generate low-resolution proxies and still frames on demand and reuse them across stages.

### 6.2 Stage B: local transition detection

Use PySceneDetect locally:

- `AdaptiveDetector(adaptive_threshold=3.0, min_content_val=15.0, window_width=2, min_scene_len=2 frames)` for hard cuts and changes amid camera motion;
- `ThresholdDetector(threshold=12, fade_bias=0.0, min_scene_len=2 frames)` for fades to/from black;
- the content/adaptive signal around sustained changes to identify dissolves.

These are versioned, administrator-configurable defaults. Run detection at reduced spatial resolution but without skipping temporal frames. Do not set a three-second minimum scene length in the detector; short shots still contain real boundaries and must be detected before duration filtering.

Threshold detection alone does not cover arbitrary cross-dissolves. Implement a local gradual-transition classifier over the content-score series:

1. Calculate the median content score from the surrounding two-second neighborhood, excluding the central half-second on each side.
2. Treat at least three consecutive elevated frames as a gradual-transition run when their score is at least `max(8, 1.5 × neighborhood median)`. Allow one below-threshold frame inside a run.
3. Treat a one- or two-frame peak as a hard-cut event.
4. Before accepting a dissolve, estimate one global affine transform between adjacent analysis frames using RANSAC feature matches. If at least 70% of tracked features fit the transform and the median motion-compensated luma difference is below 12 on an 8-bit scale, classify the run as coherent camera motion and veto the dissolve.
5. Cap a gradual-transition search at two seconds. If it remains ambiguous beyond that limit, conservatively retain the two-second region centered on the highest score as the transition interval.
6. For a fade event, expand around the event until median frame-luma change is below two 8-bit levels for five consecutive frames and the absolute fitted luma slope across those frames is below one level per frame. Search at most three seconds in each direction.

Represent transitions as intervals. Merge overlapping events, or events separated by no more than three source frames, by taking their union and retaining all contributing detector labels. Preserve known transition intervals for fades and dissolves instead of reducing them to a single unsafe frame. The detector layer MUST be behind an interface so a later local detector can be added without changing job or candidate schemas.

PySceneDetect documents adaptive/content detection for cuts and threshold detection for fades: [PySceneDetect documentation](https://www.scenedetect.com/docs/head/).

### 6.3 Stage C: construct safe intervals

For each detected shot:

1. Start with the source shot interval.
2. Remove any detected fade/dissolve interval at its edges.
3. Add an inward guard of two source frames after every internal transition interval, including hard cuts, fades, and dissolves.
4. Do not add an artificial guard at the beginning or end of the source unless a transition exists there.
5. Quantize the result to valid source-frame timestamps.
6. Calculate usable duration from the resulting timestamps.

If usable duration is below 3.0 seconds, mark the shot ineligible with reason `too_short_after_transition_guard`. Never merge it with a neighboring shot to make a longer clip.

### 6.4 Stage D: recommended interval

- If usable duration is from 3.0 through 6.0 seconds, recommend the complete safe interval.
- If usable duration exceeds 6.0 seconds, the shot is a long-shot candidate and MUST eventually recommend one, and only one, six-second interval.
- The recommended interval MUST remain inside the safe interval.
- A source video containing a single continuous long shot can produce only one clip, regardless of the requested target.

### 6.5 Stage E: local feature preparation

For every eligible candidate:

- extract three uniformly spaced frames for shots up to 12 seconds, five for shots over 12 and up to 60 seconds, and nine for shots over 60 seconds;
- keep all samples inside the safe interval and away from transition guards;
- calculate normalized local measurements for usable exposure, non-black content, non-frozen content, focus, and motion variation;
- calculate a deterministic local score:

```text
localScore =
  0.30 × exposureScore +
  0.25 × nonBlackScore +
  0.20 × nonFreezeScore +
  0.15 × focusScore +
  0.10 × motionVariationScore
```

These measurements affect ordering only. No candidate is rejected for blur, shake, or motion.

Feature calculations are deterministic and versioned:

- Decode grayscale analysis samples at 2 fps, capped at 240 uniformly spaced frames per candidate. A six-second window therefore uses up to 12 samples.
- Normalize decoded luma to an 8-bit 0–255 range before measurement.
- `exposureScore = 100 × clamp(1 - meanClippedPixelFraction / 0.50, 0, 1)`, where clipped pixels have luma at or below 8 or at or above 247.
- `nonBlackScore = 100 × fractionOfFrames(meanLuma >= 16)`.
- `nonFreezeScore = 100 × fractionOfAdjacentPairs(meanAbsoluteLumaDifference >= 1.5)` after compensating for the global affine transform. A single-frame sample receives 50.
- Compute `focusScore` from the median `log1p(varianceOfLaplacian)` and `motionVariationScore` from median motion-compensated interframe luma difference. Normalize each to 0–100 using the 5th and 95th percentiles across all eligible candidates in the job, clamp outside that range, and return 50 when both percentiles are equal.
- Persist the feature-algorithm version with the candidate and include it in analysis cache keys.

Create JPEG contact sheets at quality 82:

- each candidate storyboard contains its opaque ID, source timecode, duration, and representative frames;
- one sheet contains at most 12 candidates;
- the sheet must fit inside 2048×2048 pixels;
- one coarse Gemini request may include up to eight sheets, or 96 candidates;
- the complete inline request must remain below a configurable 18 MiB payload limit;
- source filenames and image metadata MUST be removed.

### 6.6 Stage F: coarse Gemini ranking

Use the official Gemini SDK through a backend-only provider adapter. Default model configuration:

```text
GEMINI_MODEL=gemini-3.8-flash
GEMINI_REQUEST_CAP=8
```

Both values are administrator-configurable. Pin the SDK, but keep the model identifier runtime-configurable so deployments can migrate without changing the data model.

Use temperature `0`, disable tools/grounding, and require structured JSON. Treat text visible in frames and the quoted focus prompt as selection data, never as instructions. Gemini receives contact sheets, the optional focus prompt, and explicit instructions that all candidates have already passed the continuity gate. It returns one top-level object with exactly one result for every supplied candidate:

```json
{
  "results": [
    {
      "candidateId": "uuid",
      "relevance": 0,
      "interest": 0,
      "clarity": 0,
      "confidence": 0.0,
      "motionAmbiguous": false,
      "reasonCode": "strong_visual",
      "reason": "Short explanation, no more than 160 characters."
    }
  ]
}
```

Rules:

- `relevance`, `interest`, and `clarity` are integers from 0–100.
- `relevance` is `null` when no focus prompt was provided.
- `confidence` is from 0.0–1.0.
- `reasonCode` is one of `strong_visual`, `prompt_match`, `clear_composition`, `weak_relevance`, or `low_clarity`.
- `reason` must be plain text and is escaped before rendering.
- Unknown candidate IDs, duplicates, missing candidates, and out-of-range values invalidate the response.

Final score:

```text
with focus prompt:
  geminiScore = 0.50 × relevance + 0.30 × interest + 0.20 × clarity

without focus prompt:
  geminiScore = 0.60 × interest + 0.40 × clarity

for an API-scored candidate:
  finalScore = 0.85 × geminiScore + 0.15 × localScore

for a local-only candidate:
  finalScore = localScore
```

Sort by final score descending, confidence descending, and source start ascending. The no-cut rule remains a separate eligibility gate and is not part of this score.

### 6.7 Stage G: request budgeting

Every actual Gemini model-inference request, including retries, counts against the job’s snapshotted request cap. Provider file upload/status/delete operations are recorded separately and do not consume an inference unit; the five-candidate proxy limit constrains their use.

Before any outbound model request, atomically reserve one budget unit and persist a unique attempt ID. Mark that attempt successful or failed after the response. If a worker dies after transmission but before recording the result, the indeterminate reservation remains consumed; retrying requires another available unit. This conservative rule keeps the cap valid across crashes.

Allocate the snapshotted cap deterministically:

1. A cap of `0` performs local-only ranking.
2. If the cap is at least `3`, reserve one unit for a possible proxy-video fallback batch.
3. If at least one long-shot candidate exists and at least two non-proxy units remain, reserve one unit for fine long-shot selection.
4. Assign every remaining unit to coarse contact-sheet ranking; a positive cap always assigns at least one coarse unit.
5. A retry happens immediately and consumes another unit from that stage’s allocation. If none remains, do not retry.
6. After coarse ranking, unused coarse units roll forward to fine analysis. After fine analysis, unused fine units roll forward to proxy review. Unused proxy capacity is not spent merely to exhaust the cap.

If the coarse candidate set exceeds available request capacity:

- retain as many candidates as the contact-sheet budget permits;
- select 75% of those slots by descending local score;
- select the remaining 25% by stratified source-timeline coverage;
- retain all unsent candidates as local-fallback candidates rather than deleting them;
- clearly state that prompt relevance was not evaluated for unsent candidates.

If Gemini is disabled, unavailable, invalid, over quota, or out of budget, finish ranking with local scores. A local-only candidate receives confidence `0.35` and `promptRelevanceEvaluated=false`. Apply the same global `finalScore`, confidence, and source-time sort to every candidate regardless of scoring source. Mark fallback results with a user-visible reason; do not automatically place all API-scored candidates ahead of stronger local candidates.

Transient timeouts, HTTP 429, and transient 5xx responses receive bounded exponential backoff with jitter and at most one retry. Authentication and other permanent 4xx failures are not retried.

### 6.8 Stage H: fine selection for long shots

Fine selection runs only for long-shot candidates that can affect the selected target, using a shortlist of:

```text
min(max(targetClipCount × 2, 40), 200)
```

Take that many candidates from the provisional overall ranking first, then filter the shortlist to long shots. For each shortlisted long shot:

1. Generate six-second windows beginning every one second across the safe interval.
2. Add a final window anchored to the safe interval’s end if the one-second sequence does not already include it.
3. Score every window locally using the Stage E measurements.
4. Retain at most 24 windows: half by highest local score and half evenly spaced across the full shot, then deduplicate.
5. Create timestamped storyboards that identify both the candidate ID and a server-generated window ID.
6. Require Gemini to return one supplied window ID; it may not invent a timestamp.
7. Quantize the selected window to source-frame timestamps and verify that it remains inside the safe interval and differs from six seconds by no more than one source-frame duration.

Batch long-shot storyboards within the remaining request budget. When the budget is exhausted or the response is invalid, select the retained six-second window with the highest local score. Never produce multiple clips from the same long shot.

Fine analysis returns:

```json
{
  "choices": [
    {
      "candidateId": "uuid",
      "windowId": "server-issued-id",
      "confidence": 0.0,
      "motionAmbiguous": false,
      "reason": "Short explanation."
    }
  ]
}
```

There must be exactly one valid supplied window ID for every requested long shot.

### 6.9 Stage I: rare proxy-video fallback

Gemini video analysis is allowed only when:

- the candidate is provisionally ranked within the first `targetClipCount + 5` candidates; and
- Gemini returns `motionAmbiguous=true` or confidence below `0.55`; and
- request capacity remains.

Additional rules:

- upload no more than five unique proxy candidates per job;
- prioritize candidates by lowest confidence, then highest provisional rank;
- proxies are H.264, maximum 640×360, low bitrate, and cover the selected six-second window plus up to two seconds on each side while staying inside the same shot;
- always strip proxy audio; audio/transcription analysis is outside the MVP;
- combine candidates into one request where the active Gemini API supports it;
- provide server-generated start-option IDs at half-second increments and require Gemini to return one of them;
- delete remote proxy files immediately after a validated response or failure cleanup;
- count retries against the request cap;
- fall back locally when the cap is reached.

Gemini documents direct video input, custom frame sampling, and clipping intervals: [Gemini video understanding](https://ai.google.dev/gemini-api/docs/video-understanding).

### 6.10 Stage J: cache and finalize ranking

The analysis cache key contains:

```text
source SHA-256
+ normalized focus prompt
+ exact Gemini model identifier
+ detector configuration version
+ analysis-pipeline version
+ all ranking settings that alter results
```

- Commit a cache entry only after schema and boundary validation.
- A complete cache hit MUST make zero Gemini requests.
- A partial cache MAY reuse valid coarse or fine results.
- Cache status and scoring source are orthogonal: a cached result retains whether it originally came from a contact sheet, proxy video, or local fallback.
- Changing the prompt, model, detector configuration, or scoring version invalidates affected cache entries.
- Raw secret values are never part of a cache key or record.

Rank candidates by final score, then automatically select up to the target. The review page may initially display score order, while the default export order remains source chronology.

---

## 7. System Architecture

### 7.1 Technology choices

| Component | Required technology |
|---|---|
| Web client | React + TypeScript + Vite |
| API | FastAPI + Python |
| Persistence | SQLAlchemy 2 + Alembic + SQLite in WAL mode |
| Queue | Redis + RQ |
| Media | FFmpeg + ffprobe |
| Boundary detection | PySceneDetect |
| Gemini client | Official Google Gen AI SDK behind an adapter |
| Deployment | Docker Compose |
| TLS/reverse proxy | Caddy |
| Automated tests | Pytest, Vitest, and Playwright |

Pin all runtime dependencies and commit lock files. Media binaries and detector versions MUST be identifiable in job diagnostics so cached results can be invalidated safely.

### 7.2 Deployable services

Docker Compose SHOULD contain:

- `frontend`: builds and serves the React application;
- `api`: authentication, REST endpoints, SSE, database access, and download streaming;
- `worker`: RQ analysis/export workers and local media tools;
- `redis`: queue, leases, and ephemeral coordination;
- `proxy`: same-origin routing and HTTPS termination.

Run application containers as non-root with no privileged capabilities. The API and worker share the application-data volume; source media and exports remain outside the static web root.

### 7.3 Persistent storage

Suggested layout:

```text
/data/
├── app.sqlite3
├── uploads/{uploadId}/
│   ├── source.{ext}
│   └── upload-state.json
├── jobs/{jobId}/
│   ├── probe/
│   ├── detection/
│   ├── contact-sheets/
│   ├── previews/
│   └── attempts/
├── exports/{jobId}/{exportId}/
└── cache/{cacheKey}/
```

- Store only server-generated path components.
- Store paths in the database relative to `/data`.
- Write intermediate and exported files into attempt-specific temporary directories.
- Atomically rename a validated result into its published location.
- Check available disk space before proxy generation and before export.
- Successful source files and exports remain until manual deletion.
- Failed/cancelled attempt-only files MAY be cleaned after a documented grace period.
- A ready upload MAY back multiple jobs. Reference-count its source file and shared cache entries.
- Deleting a job tombstones it synchronously, cancels work, and schedules removal of previews, contact sheets, local proxies, attempts, and exports. Decrement source/cache references and remove those files only when the final reference is gone.
- Persist every Gemini remote-file identifier and cleanup status before upload. Retry remote deletion after crashes until the provider confirms deletion or the file expires.

### 7.4 Required database records

At minimum:

- `settings`;
- `sessions`;
- `uploads`;
- `jobs`;
- `job_events`;
- `detected_shots`;
- `candidate_shots`;
- `selected_clips`;
- `analysis_usage`;
- `analysis_cache`;
- `provider_files`;
- `exports`;
- `export_files`;
- `worker_leases`;
- `audit_events`.

Use database transactions for state changes, review revisions, usage counters, and checkpoint publication.

### 7.5 Configuration

Provide `.env.example` without secrets:

```dotenv
APP_BASE_URL=https://clips.example.internal
APP_ENCRYPTION_KEY=
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=
DATA_DIR=/data
MAX_UPLOAD_BYTES=21474836480
UPLOAD_CHUNK_BYTES=16777216
GEMINI_MODEL=gemini-3.8-flash
GEMINI_REQUEST_CAP=8
SESSION_IDLE_MINUTES=60
SESSION_ABSOLUTE_HOURS=12
RETENTION_ENABLED=false
LOG_LEVEL=INFO
```

`APP_ENCRYPTION_KEY` is a base64-encoded 32-byte random value. The application MUST refuse startup if the encryption key, administrator username, or administrator password hash is missing or malformed.

---

## 8. REST API Contract

### 8.1 Conventions

- Base path: `/api/v1`.
- Paths in the endpoint tables below are relative to this base path.
- JSON fields use `camelCase`.
- Resource IDs are UUIDv7.
- Timestamps are RFC 3339 UTC.
- Media positions are integer microseconds.
- Every media interval is half-open: `[startUs, endUs)`. Its duration is exactly `endUs - startUs`. Server-side frame quantization always moves bounds inward and may change the requested duration by no more than one source-frame duration.
- Created resources return `201 Created`.
- Accepted asynchronous work returns `202 Accepted`.
- Successful mutations without bodies return `204 No Content`.
- Upload completion, job creation, cancellation/retry, review replacement, export creation, and job deletion accept an `Idempotency-Key`. Login, logout, session reads, and upload chunks do not use the generic mechanism.
- Bind an idempotency record to the authenticated principal, method, route, and canonical request-body fingerprint. Replaying the same key and fingerprint returns the original status/body; reusing the key with a different fingerprint returns `409 idempotency_conflict`.
- Retain an idempotency record for at least the lifetime of the resource it created, and never less than 24 hours. Upload chunks use their offset/checksum semantics instead.

Standard error:

```json
{
  "error": {
    "code": "invalid_job_state",
    "message": "The job must be review-ready before export.",
    "retryable": false,
    "requestId": "uuid",
    "details": {}
  }
}
```

Use:

- `401` for missing/expired authentication;
- `403` for invalid CSRF/origin or forbidden access;
- `404` for an unknown resource;
- `409` for state, review-revision, or upload-offset conflicts;
- `413` for an oversized upload;
- `415` for unsupported media;
- `422` for validation errors;
- `429` for endpoint rate limiting;
- `503` for an unavailable dependency;
- `507` for insufficient storage.

### 8.2 Authentication and settings

| Method | Path | Request and result |
|---|---|---|
| `POST` | `/auth/login` | `{ "username", "password" }` → `204`; creates and rotates the server session. |
| `POST` | `/auth/logout` | Invalidates current session → `204`. |
| `GET` | `/session` | `{ "authenticated", "csrfToken" }`; UI keeps CSRF token in memory only. |
| `GET` | `/settings/gemini` | Returns `{ "configured", "model", "requestCap", "updatedAt" }`, never the key. |
| `POST` | `/settings/gemini/test` | `{ "apiKey", "model" }` → sanitized validity result; does not persist. |
| `PUT` | `/settings/gemini` | `{ "apiKey", "model", "requestCap" }`; cap must be an integer from 0–50; validates, encrypts, and replaces settings. |
| `DELETE` | `/settings/gemini` | Deletes encrypted key material → `204`. |
| `GET` | `/settings/source-label` | Returns the global `{ "fontPreset", "fillColor", "outlineColor", "sizePercent", "updatedAt" }` defaults. |
| `PUT` | `/settings/source-label` | Validates and replaces the global typography defaults used by new jobs. |

Rate-limit login and key-test routes. Do not log their bodies.

### 8.3 Uploads

| Method | Path | Request and result |
|---|---|---|
| `POST` | `/uploads` | `{ "fileName", "sizeBytes", "mimeType", "sha256"? }` → `201 Upload`, including `chunkSizeBytes`. |
| `HEAD` | `/uploads/{uploadId}` | Returns `Upload-Offset`, `Upload-Length`, and `Upload-Status`. |
| `PUT` | `/uploads/{uploadId}/chunks` | Raw sequential bytes with `Upload-Offset`, `Content-Length`, and `Upload-Checksum: sha256 {base64Digest}` → `204` with the new `Upload-Offset`. |
| `POST` | `/uploads/{uploadId}/complete` | Verifies length/hash and starts media verification → `202 Upload`. |
| `GET` | `/uploads/{uploadId}` | Returns `Upload` with state, verified offset, progress, and sanitized error. |
| `DELETE` | `/uploads/{uploadId}` | Removes incomplete/unreferenced upload → `204`; referenced upload returns `409`. |

Upload states: `created`, `uploading`, `verifying`, `ready`, `failed`.

When the optional client whole-file hash disagrees with the authoritative server hash, completion returns `422 upload_hash_mismatch` and retains the server hash only in protected job metadata. Offset conflicts include `details.expectedOffset`; mismatched retransmissions use `409 upload_chunk_mismatch`.

### 8.4 Jobs and review

| Method | Path | Request and result |
|---|---|---|
| `GET` | `/jobs?cursor=&limit=` | Cursor-paginated job summaries ordered newest first; `limit` defaults to 25 and is capped at 100. |
| `POST` | `/jobs` | `{ "uploadId", "targetClipCount"?, "contentPrompt"?, "sourceName"?, "useGemini"? }`; upload must be `ready` → `202 Job`. |
| `GET` | `/jobs/{jobId}` | Returns current job, progress, usage, warnings, partial-result detail, and latest export. |
| `GET` | `/jobs/{jobId}/events` | Authenticated SSE with `Last-Event-ID` recovery. |
| `POST` | `/jobs/{jobId}/cancel` | Idempotently requests cancellation → `202 Job`. |
| `POST` | `/jobs/{jobId}/retry` | Resumes a retryable failed job from checkpoint → `202 Job`. |
| `GET` | `/jobs/{jobId}/candidates` | Returns `{ "candidates", "selectedClips", "reviewRevision" }` in ranked order. |
| `GET` | `/jobs/{jobId}/candidates/{candidateId}/preview` | Authenticated range-enabled preview stream. |
| `PUT` | `/jobs/{jobId}/review` | Atomically replaces selection and returns `{ "reviewRevision", "clips" }`. |
| `DELETE` | `/jobs/{jobId}` | Tombstones the job immediately and returns `204`; physical cleanup continues internally. |

Review request:

```json
{
  "revision": 3,
  "clips": [
    {
      "candidateId": "uuid",
      "order": 1,
      "startUs": 12500000,
      "endUs": 18500000
    }
  ]
}
```

SSE event types:

- `job.updated`;
- `candidates.ready`;
- `export.ready`;
- `job.failed`;
- `heartbeat`.

Each SSE message has a persisted, monotonically increasing event ID and this data envelope:

```json
{
  "sequence": 42,
  "type": "job.updated",
  "jobId": "uuid",
  "occurredAt": "2026-09-13T18:30:00Z",
  "payload": {}
}
```

Retain job events until the job is deleted. `Last-Event-ID` resumes after the last delivered sequence; an unknown or expired sequence returns the current job snapshot before live events.

A stale review revision returns `409 stale_review_revision` with `details.currentRevision`, `details.selectedClips`, and no partial mutation.

### 8.5 Exports

| Method | Path | Request and result |
|---|---|---|
| `POST` | `/jobs/{jobId}/exports` | `{ "reviewRevision", "resolutions", "includeAudio"? }`; audio defaults to `true` → `202 Export`. |
| `GET` | `/jobs/{jobId}/exports/{exportId}` | Returns export state, progress, error, and manifest summary. |
| `POST` | `/jobs/{jobId}/exports/{exportId}/cancel` | Cooperatively cancels a queued/running export → `202 Export`. |
| `POST` | `/jobs/{jobId}/exports/{exportId}/retry` | Requeues a failed export from its validated ledger → `202 Export`. |
| `GET` | `/jobs/{jobId}/exports/{exportId}/download` | Streams the completed ZIP with range support. |

`resolutions` is a unique, nonempty subset of:

```json
["original", "max1080p", "max720p"]
```

At least one reviewed clip is required. Only one export can run per job. Starting a later export from a completed job is allowed.

Export states are `queued`, `exporting`, `complete`, `failed`, and `cancelled`. The first export moves the job from `review-ready` to `exporting`; success moves it to `complete`, while failure/cancellation returns it to `review-ready`. A re-export temporarily moves a complete job to `exporting`; success, failure, or cancellation returns it to `complete`. The `Export` records its own terminal state, and prior successful exports remain downloadable. Job cancellation during `exporting` returns `409 export_active`; callers use the export-specific cancellation endpoint.

---

## 9. Core Data Schemas

These TypeScript-style contracts are normative for API behavior. Equivalent backend models must validate the same constraints.

```ts
type UploadState = "created" | "uploading" | "verifying" | "ready" | "failed";

interface Upload {
  id: string;
  fileName: string;
  declaredSizeBytes: number;
  verifiedOffsetBytes: number;
  chunkSizeBytes: number;
  state: UploadState;
  sha256: string | null;
  error: JobError | null;
  createdAt: string;
  updatedAt: string;
}

interface JobList {
  items: JobSummary[];
  nextCursor: string | null;
}

interface JobSummary {
  id: string;
  sourceFileName: string;
  state: JobState;
  progressPercent: number;
  targetClipCount: number;
  selectedCount: number;
  latestExport: ExportSummary | null;
  createdAt: string;
  updatedAt: string;
}

type JobState =
  | "uploaded"
  | "probing"
  | "detecting"
  | "ranking"
  | "review-ready"
  | "exporting"
  | "complete"
  | "failed"
  | "cancelled";

interface Job {
  id: string;
  uploadId: string;
  state: JobState;
  targetClipCount: number;
  contentPrompt: string | null;
  sourceLabel: {
    text: string;
    style: {
      fontPreset: "bebas-neue" | "anton" | "oswald-semibold" | "roboto-condensed-bold";
      fillColor: string;
      outlineColor: string;
      sizePercent: number;
    };
  } | null;
  useGemini: boolean;
  video: {
    sha256: string;
    durationUs: number;
    width: number;
    height: number;
    averageFrameRate: string;
    hasAudio: boolean;
  } | null;
  progress: {
    phase: JobState;
    percent: number;
    message: string;
  };
  eligibleCount: number | null;
  selectedCount: number;
  reviewRevision: number;
  latestExport: ExportSummary | null;
  partialResultReason: string | null;
  warnings: string[];
  usage: AnalysisUsage;
  error: JobError | null;
  createdAt: string;
  updatedAt: string;
}

interface JobError {
  phase: JobState | ExportState | UploadState;
  code: string;
  message: string;
  retryable: boolean;
  occurredAt: string;
}

interface TransitionBoundary {
  startUs: number;
  endUs: number;
  kinds: Array<"hard-cut" | "fade" | "dissolve">;
}

interface CandidateShot {
  id: string;
  jobId: string;
  shotNumber: number;
  sourceStartUs: number;
  sourceEndUs: number;
  safeStartUs: number;
  safeEndUs: number;
  usableDurationUs: number;
  recommendedStartUs: number;
  recommendedEndUs: number;
  incomingBoundary: TransitionBoundary | null;
  outgoingBoundary: TransitionBoundary | null;
  rank: number;
  score: number;
  confidence: number;
  reason: string;
  scoringSource:
    | "contact-sheet"
    | "proxy-video"
    | "local-fallback";
  cacheStatus: "none" | "partial" | "complete";
  promptRelevanceEvaluated: boolean;
  thumbnailUrl: string;
  previewUrl: string;
}

interface SelectedClip {
  id: string;
  candidateId: string;
  order: number;
  startUs: number;
  endUs: number;
  durationUs: number;
}

interface AnalysisUsage {
  model: string | null;
  requestCap: number;
  requestsUsed: number;
  coarseRequests: number;
  fineRequests: number;
  proxyVideoRequests: number;
  proxyVideoCandidates: number;
  providerFileOperations: number;
  imageBytesSent: number;
  proxyVideoBytesSent: number;
  proxyVideoSecondsSent: number;
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  cacheStatus: "none" | "partial" | "complete";
  localFallbackUsed: boolean;
  fallbackReason: string | null;
}

type ExportState = "queued" | "exporting" | "complete" | "failed" | "cancelled";

interface ExportSummary {
  id: string;
  state: ExportState;
  createdAt: string;
  completedAt: string | null;
}

interface Export {
  id: string;
  jobId: string;
  state: ExportState;
  reviewRevision: number;
  resolutions: Array<"original" | "max1080p" | "max720p">;
  includeAudio: boolean;
  progress: {
    percent: number;
    message: string;
  };
  error: JobError | null;
  manifestAvailable: boolean;
  downloadAvailable: boolean;
  createdAt: string;
  updatedAt: string;
  completedAt: string | null;
}

interface ExportManifest {
  schemaVersion: "1.1";
  jobId: string;
  exportId: string;
  createdAt: string;
  source: {
    fileName: string;
    sha256: string;
    durationUs: number;
    width: number;
    height: number;
    averageFrameRate: string;
    hasAudio: boolean;
  };
  options: {
    resolutions: Array<"original" | "max1080p" | "max720p">;
    includeAudio: boolean;
    sourceLabel: {
      text: string;
      style: {
        fontPreset: "bebas-neue" | "anton" | "oswald-semibold" | "roboto-condensed-bold";
        fillColor: string;
        outlineColor: string;
        sizePercent: number;
      };
    } | null;
  };
  clips: Array<{
    serial: number;
    candidateId: string;
    sourceStartUs: number;
    sourceEndUs: number;
    durationUs: number;
    score: number;
    confidence: number;
    reason: string;
    files: Array<{
      resolution: "original" | "max1080p" | "max720p";
      path: string;
      width: number;
      height: number;
      sizeBytes: number;
      sha256: string;
    }>;
  }>;
}
```

`Job.video.width`/`height` and manifest dimensions are display-oriented rather than coded dimensions. In each manifest clip, `sourceStartUs`/`sourceEndUs` are the selected trim, not the full candidate bounds. Every manifest file `path` is ZIP-relative. `manifest.csv` contains one row per clip-resolution pair and matches `manifest.json`. Neither manifest may contain secrets, session information, raw model output, internal filesystem paths, or protected diagnostics.

Treat every CSV string as untrusted. Prefix cells beginning with `=`, `+`, `-`, `@`, tab, or carriage return with a single quote in CSV output to prevent spreadsheet formula execution. Keep the original plain string in JSON.

---

## 10. Security Requirements

### 10.1 Application and network

- Require HTTPS for every non-loopback deployment and enable HSTS after TLS is confirmed.
- Keep the frontend and API same-origin; disable permissive CORS.
- Apply a restrictive Content Security Policy and standard frame, MIME-sniffing, referrer, and permissions headers.
- Trust proxy headers only from the configured reverse proxy.
- Rate-limit authentication and key-validation attempts.
- Record login success/failure, key replacement/deletion, job deletion, and export creation without recording secrets or media payloads.

### 10.2 Media and filesystem

- Generate all paths from server-side UUIDs.
- Block traversal, reserved names, control characters, archive path injection, and command injection.
- Invoke FFmpeg and ffprobe with argument arrays, never interpolated shell commands.
- Restrict media tools to local job files and disable unnecessary network protocols.
- Run subprocesses with CPU, memory, process, and wall-time limits.
- Never serve `/data` as a static directory. Stream previews and downloads through authenticated routes.
- Build ZIP paths from validated serials and fixed folder names.

### 10.3 Browser and model content

- Escape focus prompts and Gemini explanations when rendered.
- Do not interpret model text as HTML, Markdown with raw HTML, a command, a path, or an instruction to the application.
- Validate every Gemini response against the declared schema and known candidate/time bounds.
- Do not send full source video to Gemini.
- Disclose before the first model-assisted job that representative images, and rarely short low-resolution proxies, may be sent to Gemini.

### 10.4 Secret verification

Automated tests MUST plant a recognizable fake key and verify that it is absent from:

- API responses;
- frontend storage;
- Redis/RQ arguments;
- application and proxy logs;
- trace/error events;
- SQLite plaintext searches;
- manifests and ZIP archives.

Tampering with saved ciphertext, nonce, tag, or authenticated metadata MUST make decryption fail safely.

---

## 11. Failure Semantics

| Failure | Required behavior |
|---|---|
| Invalid/corrupt media | Fail verification with a safe, non-retryable explanation; retain incomplete upload only until user deletion or cleanup grace period. |
| Insufficient eligible shots | Reach `review-ready`, return all eligible shots, and show requested/delivered counts. |
| Invalid Gemini key | Preserve local detection, complete ranking with local fallback, and show how to replace the key. |
| Gemini timeout/429/5xx | Retry once if budget remains, then use local fallback. |
| Gemini malformed output | Reject response, retry once if budget remains, then use local fallback. |
| Request-cap exhaustion | Stop external calls, finish with cached/local scores, and label reduced confidence. |
| Disk full | Stop safely, remove attempt-only files, preserve source/checkpoints, and return retryable `insufficient_storage`. |
| FFmpeg/detector failure | Capture protected diagnostics, publish no partial output, and allow retry when safe. |
| Worker death/restart | Reclaim expired lease and resume last incomplete stage without duplicate candidates. |
| Browser disconnect | Continue the job; recover through status and SSE reconnect. |
| Analysis cancellation | Terminate active media work, publish no partial analysis result, retain the source/checkpoints, and set the job to `cancelled`. |
| Export cancellation | Stop the export, publish no partial ZIP, mark the `Export` cancelled, and restore the job’s prior `review-ready` or `complete` state. |
| Job deletion | Tombstone immediately, hide the job from APIs, and retry local/remote cleanup until every unreferenced derivative is removed. |
| ZIP validation failure | Keep prior successful exports intact and mark only the new export attempt failed. |

User-facing errors use stable codes and actionable messages. Never log request bodies, inline images/videos, decrypted keys, environment values, authorization headers, cookies, or raw provider payloads. Logs may contain only request IDs, byte counts, status codes, timings, tool exit codes, and sanitized error categories; protected diagnostic logs follow the same prohibition.

---

## 12. Test Plan

Use deterministic synthetic fixtures with frame-accurate transition annotations in normal CI. Mock Gemini for all routine tests. Keep a separate opt-in credentialed smoke test using a tiny fixture and a strict request cap.

### 12.1 Unit tests

- Boundary merging within the three-frame tolerance.
- Fade/dissolve exclusion and two-frame guards after every internal transition.
- Exact 3-second, exact 6-second, shorter-than-3-second, and longer-than-6-second shot rules.
- Rational frame-rate and microsecond conversions, including 30000/1001.
- Safe clamping and quantization of recommended and reviewed intervals.
- One clip maximum per detected shot.
- Target count validation and partial-result calculation.
- Ranking formulas with and without a focus prompt.
- Local fallback and request-budget allocation.
- Cache-key normalization and invalidation.
- Gapless serial numbering after deselection/reorder.
- Resolution bounding and even-dimension calculations without upscaling.
- Encryption/decryption, nonce uniqueness, tamper detection, and startup validation.

### 12.2 Media integration matrix

Test:

- hard cuts;
- fades to/from black;
- dissolves;
- continuous pans and zooms;
- handheld shake;
- blur and rapid motion;
- one uninterrupted long take;
- adjacent short shots;
- portrait, landscape, and square footage;
- fractional and variable frame rates;
- rotated video;
- silent video and several audio formats;
- malformed, truncated, and unsupported files.

Assertions:

- motion, shake, and blur remain eligible when no transition occurs;
- no output crosses an annotated transition;
- all outputs decode completely;
- orientation and aspect ratio are correct;
- audio remains synchronized when included and is absent when muted.

### 12.3 Upload tests

- Fresh, interrupted, resumed, duplicate, and completed uploads.
- Identical chunk retransmission.
- Wrong offset, checksum mismatch, overlap mismatch, malformed range, and oversized body.
- Spoofed MIME/extension, malicious filename, and unsupported/corrupt media.
- Service restart during transfer and verification.

### 12.4 Gemini and ranking tests

- No-prompt and prompt-relevance ranking.
- Valid, missing, duplicate, unknown-ID, malformed, and out-of-range responses.
- Low confidence and `motionAmbiguous`.
- HTTP 401/403, 429, timeout, and 5xx behavior.
- Retry counting against the request cap.
- Worker termination after durable request reservation but before provider response recording; the indeterminate reservation remains consumed.
- Validation of request caps `0`, `8`, and `50`, plus rejection of negative, fractional, and greater-than-50 values.
- Five-candidate proxy maximum.
- Request-cap exhaustion and local fallback.
- Complete cache hit with zero model requests.
- Cache invalidation after prompt, model, detector, or pipeline changes.

### 12.5 Review and export tests

- Preview authorization and range requests.
- Selection, deselection, drag reorder, restore default, and stale revision conflict.
- Minimum/maximum legal trim and every illegal-boundary case.
- Original, max-1080p, and max-720p for large and small portrait/landscape sources.
- Multi-resolution export, gapless names, JSON/CSV parity, and ZIP safety.
- CSV formula neutralization for model reasons beginning with `=`, `+`, `-`, `@`, tab, and carriage return.
- Export with source audio, muted export, and silent source.
- Re-export after completion with different settings.
- Labelled and unlabelled previews/exports, settings snapshots across global
  changes and re-exports, long-name fitting, portrait/landscape placement, and
  hostile punctuation remaining text-file data rather than filter syntax.
- Cancellation and worker termination during every export stage.
- Job deletion tombstoning plus eventual removal of previews, sheets, local/remote proxies, attempts, exports, and unreferenced source/cache entries.

### 12.6 Security tests

- Correct/incorrect password, throttling, session rotation/expiry, and logout invalidation.
- Missing/invalid CSRF and cross-origin mutation attempts.
- Exact cookie attributes, HSTS only after TLS, CSP/security headers, disabled cross-origin credentials, and spoofed forwarding-header rejection.
- Unauthorized and guessed preview/download URLs.
- Path traversal, shell metacharacters, reserved filenames, malicious metadata, and ZIP injection.
- Script content in focus prompts and model reasons.
- Wrong/missing encryption master key and ciphertext tampering.
- Fake-key scans across logs, responses, browser storage, Redis, SQLite, manifests, and ZIP files.
- Assertions that request bodies, media payloads, provider payloads, environment values, and cookies never enter diagnostic logs.

### 12.7 End-to-end scenario

1. Sign in and save a test Gemini key.
2. Start an upload, interrupt it, and resume from the verified offset.
3. Analyze a fixture containing cuts, a dissolve, short shots, and a long shot.
4. Confirm progress survives an SSE disconnect.
5. Confirm eligible candidates and any partial-result explanation.
6. Preview, deselect, reorder, and legally trim clips.
7. Attempt and reject an out-of-bound trim.
8. Export several resolutions with audio.
9. Validate each media file, serial numbering, both manifests, and the ZIP.
10. Restart services and retrieve the completed job.

---

## 13. Acceptance Criteria

The MVP is accepted only when all of the following are true:

1. Every exported clip is 3.0–6.0 seconds inclusive within one source-frame tolerance.
2. Every clip remains inside one detected shot’s safe interval and crosses none of the annotated fixture transitions.
3. Every selected shot contributes at most one clip.
4. A selected 3–6 second shot retains its safe duration; a selected longer shot produces exactly one six-second clip.
5. A job with fewer eligible shots succeeds and reports requested count, delivered count, and exclusions.
6. Original/1080p/720p outputs preserve aspect ratio and orientation and never exceed source dimensions.
7. Every output passes `ffprobe` validation and decodes to completion.
8. Audio-enabled exports meet the defined stream start/end synchronization tolerance for CFR and VFR sources; muted exports contain no audio stream.
9. Review order controls serial numbering; otherwise source chronology is used. Every resolution folder has matching, gapless names.
10. JSON and CSV manifests match the generated files and probed media values.
11. A valid complete cache hit performs zero Gemini requests.
12. Every job remains within its snapshotted request cap and uploads no more than five unique proxy candidates.
13. Gemini errors never erase local analysis; locally viable candidates still reach review with a clear fallback label.
14. An interrupted upload resumes without retransmitting verified bytes.
15. Killing a worker or restarting services resumes from a committed checkpoint without duplicate candidates or corrupt published output.
16. Cancellation terminates active processing and leaves no attempt-only published files.
17. Every authenticated state-changing route enforces authentication, same-origin checks, and CSRF protection; login enforces origin/Fetch-Metadata checks and throttling.
18. A planted Gemini-key sentinel is absent from logs, responses, browser storage, queue payloads, manifests, and ZIPs.
19. Persisted Gemini key material is authenticated ciphertext, and any tampering causes safe decryption failure.
20. The complete end-to-end scenario passes in CI with Gemini mocked.

---

## 14. Recommended Implementation Order

### Milestone 1: secure foundation and uploads

- Docker Compose, configuration validation, database migrations, shared login, sessions, CSRF, and encryption.
- Resumable upload API, browser uploader, SHA-256 verification, ffprobe validation, and upload recovery tests.

### Milestone 2: deterministic local analysis

- Job queue/state machine, persisted events, SSE progress, cancellation, retry, and worker leases.
- PySceneDetect adapter, safe-interval calculation, local measurements, thumbnails, previews, and synthetic detection fixtures.

### Milestone 3: Gemini-assisted ranking

- Contact-sheet generator, provider adapter, schema-constrained responses, scoring, request accounting, cache, fine long-shot selection, and capped proxy fallback.
- Fully mocked provider tests before any credentialed smoke test.

### Milestone 4: review and export

- Ranked review interface, revision-safe selection/reorder/trim controls.
- Frame-accurate FFmpeg exports, resolution/audio choices, validation, manifests, ZIP packaging, and downloads.

### Milestone 5: recovery and hardening

- Restart/retry fault injection, disk-full behavior, media sandboxing, rate limiting, security headers, audit events, secret scans, and complete end-to-end automation.

Do not begin public SaaS, platform-link imports, social crops, or automatic reframing until the MVP acceptance criteria pass.

---

## 15. Source References

- [PySceneDetect documentation](https://www.scenedetect.com/docs/head/)
- [FFmpeg documentation](https://ffmpeg.org/ffmpeg.html)
- [Gemini video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- [Gemini API key security](https://ai.google.dev/gemini-api/docs/api-key)
