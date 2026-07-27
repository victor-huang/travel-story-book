# Implementation Tracker

Companion to [`mvp_process_from_picture_to_stories.md`](./mvp_process_from_picture_to_stories.md).
That doc is **what and why** and is the source of truth for design. This doc is **who, in what
order, and how to not collide** — it is the source of truth for progress.

If the two ever disagree, the plan doc wins on design and this doc wins on status. Amend the
plan doc rather than diverging from it in code.

---

# How multiple agents work here

## The one rule that prevents most damage

**One task, one agent, one disjoint set of files.** Every task below lists the files it
**owns**. You may *read* anything; you may *write* only files your task owns. If your task
needs a change to a file another task owns, do not edit it — record it under
[Cross-task requests](#cross-task-requests) and keep going on what you can.

## Waves

Tasks are grouped into waves. **Wave 0 is serial and blocking** — it freezes the contracts
(DB schema, stage interface, config, models) that every other task compiles against. Nothing
in Wave 1+ starts until Wave 0 is merged, or agents will build against shifting foundations
and the integration cost will exceed the parallelism gain.

Within a wave, all tasks are parallel-safe by construction. Between waves, respect
`Depends on`.

## Claiming a task

1. Check `Status` is `todo` and every `Depends on` task is `done`.
2. Edit this file: set `Status` to `wip` and put your agent name/ID in `Owner`. Make that
   edit **before** writing any code — it's the lock.
3. Work only inside your owned files.
4. When finished: tests pass, `Status` → `review` (or `done` if you also verified the
   acceptance criterion against real data), and add a line to the [Log](#log).

If you find a task already `wip` with an owner, pick another. Never take over a `wip` task
without the human's say-so.

## After each dev/test cycle

Add a retro entry to `retro/agentic_coding/` for anything that cost real time or shipped a
wrong result — above all **tests that were wrong rather than code**, and confident claims that
turned out false. Skim the most recent entry before starting a cycle. Template and conventions
are in that folder's README.

## Definition of done for any task

- Acceptance criterion in the task entry is demonstrably met.
- Unit tests exist and pass (see [Testing rules](#testing-rules)).
- `autoflake -i --remove-all-unused-imports <your files>` has been run.
- `ruff check` and `ruff format` are clean.
- No file outside your owned set was modified.
- The [Log](#log) has your entry.

## Escalate to the human, don't improvise

Stop and ask if you hit any of these:
- A Wave 0 contract file needs to change.
- The plan doc is wrong, ambiguous, or silent on something load-bearing.
- A dependency needs a paid API key or a large model download.
- Your acceptance criterion can't be met without real trip data you don't have.

---

# Repo layout (frozen by Wave 0)

```
pyproject.toml                     T01
config.example.toml                T02
overrides.example.toml             T32
src/story_book/
  __init__.py                      T01
  cli.py                           T06
  config.py                        T02
  overrides.py                     T32
  db/
    schema.sql                     T03
    connection.py                  T03
    models.py                      T03
  pipeline/
    base.py                        T04   Stage protocol + result caching
    runner.py                      T04   wave ordering, parallelism, progress
    scan.py                        T10
    metadata.py                    T11
    timezones.py                   T12
    gps_backfill.py                T20
    geocode.py                     T21
    days.py                        T22
    events.py                      T24
    embeddings.py                  T14
    dedup.py                       T23
    quality.py                     T13
    video.py                       T15
    selection.py                   T30
    timeline.py                    T31
    landmarks/
      base.py                      T25
      providers.py                 T25
  export/
    contact_sheet.py               T16
    report.py                      T40
    package.py                     T41
    templates/                     T40
  profile.py                       T17
  eval.py                          T18
tests/
  conftest.py                      T05
  fixtures/                        T05
  unit/test_<module>.py            each task owns its own file
  backend/test_<module>.py         each task owns its own file
```

Each pipeline task owns exactly `pipeline/<its file>.py`, `tests/unit/test_<name>.py`, and
`tests/backend/test_<name>.py`. That's what makes the fan-out safe.

---

# Testing rules

Per the global Python rules for this user:

- Encapsulate tests in **classes** per target module/class/method.
- **One behavior per test.** No multi-assert grab bags.
- Prefer `mocker.patch` (pytest-mock) over `unittest.mock` decorators.
- Readable test code over inline comments.
- `tests/unit/` — **no DB, no filesystem, no network.** Mock them.
- `tests/backend/` — may use a real temp SQLite DB and real fixture files, and may create the
  objects a test needs. (Name kept for consistency with the user's other projects; here it
  means "integration.")
- Keep imports at the top of the file.
- Inline comments only for genuinely complex logic or edge cases.

Every stage must have at least one `tests/backend/` test that runs it against a real fixture
file end-to-end. Mocked-only coverage of image and EXIF code is how the HEIC and timezone
bugs slip through.

---

# Wave 0 — Contracts (SERIAL, blocks everything)

One agent does all of Wave 0, in order. Do not parallelize this.

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| T01 | Repo skeleton & tooling | done | claude (main) | — |
| T02 | Config loading | done | claude (main) | T01 |
| T03 | DB schema, connection, models | done | claude (main) | T01 |
| T04 | Stage framework & runner | done | claude (main) | T03 |
| T05 | Test fixtures & conftest | done | claude (main) | T01 |
| T06 | CLI skeleton | done | claude (main) | T02, T04 |

**Wave 0 is complete and Wave 1 is fully unblocked.** 180 tests pass, 0 skip.

System binaries are installed (exiftool 13.55, ffmpeg 8.1.2), so all 21 fixtures exist
including the two video clips — T11 and T15 have no remaining blocker. The fixture generator
is deterministic: re-running it leaves the 19 image fixtures byte-identical, so a
regeneration never shows up as spurious diff noise.

### Contracts now frozen — read these before starting any Wave 1 task

| File | What it fixes |
| --- | --- |
| `src/story_book/db/models.py` | The inter-stage data contract. `Media` plus the `StrEnum`s for kind/tz_source/gps_source/cluster_kind/scope. |
| `src/story_book/db/schema.sql` | All 18 tables. `stage_result` is the resume backbone. |
| `src/story_book/db/connection.py` | `upsert_media`, `iter_media`, `completed_hashes`, `record_stage_result`. Use these; do not write raw SQL against `media` or `stage_result`. |
| `src/story_book/pipeline/base.py` | Pick one: `PerItemStage` (compute/persist split), `BatchStage` (CLIP, vision APIs), `WholeTripStage` (days, events, selection, timeline). `SkipItem` for "does not apply". `available()` to declare a missing binary. |
| `src/story_book/config.py` | Every threshold. **No magic numbers in stage code** -- add a field here instead. |

### Wave 1 task checklist

1. Set your row to `wip` with your name, in this file, before writing code.
2. Add `src/story_book/pipeline/<yours>.py` with a stage class; register it in
   `cli.py::build_stages` (that function is the one shared file Wave 1 tasks touch -- append
   only, one line, and expect to rebase).
3. Add `tests/unit/test_<yours>.py` (mocked) and `tests/backend/test_<yours>.py` (real
   fixtures). The `ctx`, `conn`, `make_media`, `media_dir`, and `source_dir` fixtures already
   exist in `tests/conftest.py`.
4. `uv run pytest && uv run ruff check . && uv run ruff format .` and autoflake your files.
5. Set `review`/`done` and add a Log line.

### T01 — Repo skeleton & tooling
**Owns:** `pyproject.toml`, `.gitignore`, `LICENSE`, `README.md`, `src/story_book/__init__.py`,
all package `__init__.py`, `.github/workflows/ci.yml`
Python 3.12, `uv`, Typer + Rich, pytest + pytest-mock, ruff, autoflake. Empty packages for
every dir in the layout above so later tasks only add files. CI on macOS + Linux.
`git init` if the repo isn't one yet.
**Done when:** `uv sync && uv run pytest` passes on an empty suite and `uv run story-book --help` works.

### T02 — Config loading
**Owns:** `src/story_book/config.py`, `config.example.toml`, `tests/unit/test_config.py`
Typed config (dataclasses or pydantic) with every tunable from the plan doc: thresholds
(`event_gap_minutes`, `event_jump_km`, `day_start_hour`, `suspicious_gap_days`,
`transcribe_min_seconds`, similarity cutoffs, `highlights_per_event`), `home_location` +
`home_exclusion_km`, per-device `clock_offset_minutes`, model names, provider settings,
`no_cloud`. Defaults live here and nowhere else — no magic numbers in stage code.
**Done when:** example config round-trips, defaults match the plan doc, unknown keys error loudly.

### T03 — DB schema, connection, models
**Owns:** `src/story_book/db/*`, `tests/backend/test_db.py`
Implement the schema from the plan doc verbatim, including `stage_result`. WAL mode, foreign
keys on, a `schema_version` row, and indexes on `media.taken_utc`, `media_event.event_id`,
`media_cluster.cluster_id`. Dataclasses in `models.py` are the inter-stage data contract —
**this is the most consequential file in the project**; every other task imports it.
**Done when:** schema creates from scratch, models round-trip through the DB, one-trip
invariant documented.

### T04 — Stage framework & runner
**Owns:** `src/story_book/pipeline/base.py`, `src/story_book/pipeline/runner.py`,
`tests/unit/test_base.py`, `tests/backend/test_runner.py`
The heart of the resumability constraint. A `Stage` protocol with `name`, `version`, and
`process`; caching keyed on `(media_hash, stage, stage_version)`; per-item commit so an
interrupt loses at most one item; `ProcessPoolExecutor` for CPU stages and `asyncio` for
network ones; Rich progress; `--dry-run` and `--force <stage>`; failures recorded per item
so one bad file never kills a run.
**Done when:** a fake 3-stage pipeline over 100 fake items, killed mid-run via `SIGINT`,
resumes and recomputes only the unfinished items. Prove it with a test.

### T05 — Test fixtures & conftest
**Owns:** `tests/conftest.py`, `tests/fixtures/**`
Tiny (<50KB each) committed real media covering: HEIC with GPS + `OffsetTimeOriginal`, JPEG
with GPS, JPEG with **no** GPS, JPEG with no EXIF at all, a burst pair, an exact duplicate
pair, two visually-distinct photos, a 3-second MOV with speech, a 3-second silent MP4, a
screenshot, a receipt photo, and two files straddling a timezone boundary. Fixtures for temp
DB and temp out-dir.
**Done when:** every case above exists and a smoke test decodes each one. **The HEIC decode
smoke test is required** — it's the dependency most likely to break on a fresh machine.

### T06 — CLI skeleton
**Owns:** `src/story_book/cli.py`, `tests/unit/test_cli.py`
Commands: `build <src> --out <dir>` (`--dry-run`, `--no-cloud`, `--force`, `--transcribe`,
`--include-all`), `report --out <dir>`, `profile <src>`. Wired to the runner with stages
registered but unimplemented, so Wave 1 agents just register theirs.
**Done when:** all three commands run against fixtures and no-op cleanly.

---

# Wave 1 — Leaf stages (fully parallel, ~8 agents)

No dependencies on each other. Each is a pure `input → output` stage over the contracts.

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| T10 | Scan & hash (M1) | done | agent-scan | Wave 0 |
| T11 | Metadata extraction (M2) | done | agent-metadata | Wave 0 |
| T12 | Timezone resolution (M2) | done | agent-timezones | Wave 0 |
| T13 | Quality scoring (M8) | done | agent-quality | Wave 0 |
| T14 | CLIP embeddings (M7) | done | agent-clip | Wave 0 |
| T15 | Video analysis (M9) | done | agent-video | Wave 0 |
| T16 | Contact sheet renderer (M14) | done | agent-contactsheet | Wave 0 |
| T17 | `profile` command (Phase 0) | done | claude (main) | Wave 0 |
| T18 | Truth set format & eval harness | done | agent-eval | Wave 0 |

### T10 — Scan & hash
BLAKE2b content hashing, extension allowlist, symlink and hidden-file handling, sidecar
skipping. **Acceptance:** scanning twice yields zero new rows the second time.

### T11 — Metadata extraction
`pyexiftool` with **one batched process for the whole run** — per-file spawn is a 20× slowdown
and is the single most common way to get this wrong. Extract every field listed in Module 2.
Missing/garbage EXIF must degrade, never crash. **Acceptance:** all fixture files yield
correct metadata; the no-EXIF fixture produces nulls without error.

### T12 — Timezone resolution
The four-level fallback from Module 2 (`OffsetTimeOriginal` → `timezonefinder` from GPS →
nearest GPS-bearing item on same device → config default), storing naive local + resolved UTC
+ `tz_source`. Detect and warn on suspicious per-device clock offsets. Highest-risk logic in
the project — test it hard. **Acceptance:** timezone-straddling fixtures land on the correct
local day and cross-device ordering is right.

### T13 — Quality scoring
Phase 1 signals only: Laplacian-variance sharpness normalized by resolution, histogram
clipping, contrast, face count + largest-face fraction, and CLIP zero-shot content class
(screenshot/receipt/document/food/landscape/group/other). Documented weighted sum for
`overall` — weights come from config. **Do not** implement smile/eyes-open/composition; those
are explicitly Phase 2. **Acceptance:** the screenshot and receipt fixtures classify
correctly; a blurred fixture scores below a sharp one.
*Note:* needs a CLIP call. Define a narrow interface and mock it in unit tests; T14 owns the
real implementation. Coordinate via [Cross-task requests](#cross-task-requests) if the
interface needs to change.

### T14 — CLIP embeddings
`open_clip` ViT-B/32, batched, MPS where available, CPU fallback. Cache vectors by content
hash in `embedding` — the most expensive local computation, and it must survive a re-run.
Also owns the shared zero-shot-classification helper T13 consumes.
**Acceptance:** embeddings are stable across runs; a re-run recomputes nothing; visually
similar fixtures score high cosine similarity and distinct ones low.

### T15 — Video analysis
FFmpeg for duration/resolution/fps/poster/keyframes/motion score; `faster-whisper` (`small`
default) for transcription with `none|auto|all`. **Checkpoint per video** — an interrupt must
lose at most one clip. **Acceptance:** both video fixtures get thumbnails and duration;
`auto` transcribes the speech clip and skips the silent one.

### T16 — Contact sheet renderer
Given a list of (image, caption) pairs, render labeled grid montages (~12–20 cells) as JPEG
with a stable cell index for the brief's mapping. Pure Pillow, no DB. **Acceptance:** a
20-image sheet is legible at typical screen size and every cell's index matches its caption.

### T17 — `profile` command ✅ done
**Owns:** `src/story_book/profile.py`, `profile_render.py`, `profile_json.py`,
`src/story_book/media_types.py`, `tests/unit/test_profile.py`, `tests/backend/test_profile.py`

Delivered: counts by type/device/extension, date range and span, timezone-offset distribution
and crossing count, % missing GPS (overall and per device), % HEIC, total bytes, video count
and total duration, inter-photo gap percentiles, late-night item count, plus a **warnings**
section and a **suggested config** table computed from observed data. `--json` writes the raw
profile for diffing. Runs standalone: no DB, no writes, degrades to file-only stats without
exiftool.

**⚠️ T10 must import the extension allowlist from `story_book/media_types.py`** rather than
redefining one — that module is shared so the scanner and profiler cannot disagree about what
counts as media. T10 may extend it; do not fork it.

P01 complete — see [`p01_profile_findings.md`](./p01_profile_findings.md) for the observed
numbers and derived config (anonymized; no paths, places, coordinates, or dates).

### T18 — Truth set format & eval harness
Define the labeled-truth-set file format (event boundaries, duplicate groups, preferred pick
per group) and an `eval` command scoring the pipeline against it: event precision/recall,
duplicate-group correctness, keeper agreement rate. **Acceptance:** runs against a
hand-written toy truth set and reports the metrics named in the plan's success criteria.

---

# Wave 2 — Location & grouping (parallel, ~5 agents)

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| T20 | GPS backfill (M3) | done | agent-gps | T11, T12 |
| T21 | Reverse geocoding (M4) | done | agent-geocode | T11 |
| T22 | Days (M5) | done | agent-days | T12 |
| T25 | Landmark provider interface (M11) | review | agent-landmarks | Wave 0 |
| T26 | Home-location privacy filter | done | agent-privacy | T11, T20 |

### T20 — GPS backfill
Time-interpolate location for GPS-less media from GPS-bearing neighbors on any device.
Record `gps_source` and confidence; refuse to interpolate across gaps > config window.
**Acceptance:** ≥90% of GPS-less fixture/trip media gets a plausible location, and
interpolated points are distinguishable from measured ones.

### T21 — Reverse geocoding
Offline-first via a bundled GeoNames extract (cities/regions, zero network). Optional
Nominatim for POI names, cached by coordinates rounded to ~50m, rate-limited per its usage
policy. **Acceptance:** every event gets a city-level label with zero network calls.

### T22 — Days
Group into local calendar days with configurable `day_start_hour`. **No trip-boundary
detection** — one trip per run, the folder is the trip. Warn (never split) on gaps >
`suspicious_gap_days`. **Acceptance:** a late-night sequence stays with the evening it began.

### T25 — Landmark provider interface
Thin provider interface + at least two implementations, batching several images per request
with coordinates and place name as context, requesting structured output (name, confidence,
description, notable features). Cache by content hash + prompt version. Print an estimated
call count and cost and require confirmation above a threshold. `--no-cloud` skips entirely.
**Acceptance:** works against a mocked provider in unit tests; `--no-cloud` leaves the
pipeline complete; the cost estimate is printed before any call.

### T26 — Home-location privacy filter
Flag `media.is_near_home` within `home_exclusion_km` of configured home; exclude from all
exports by default.

**Dependency corrected:** originally listed as depending on T21 (geocoding). It doesn't — it needs
coordinates and a configured home, which come from T11/T20. Reverse geocoding is irrelevant to a
distance test, so this can run in parallel with T21. **Acceptance:** a fixture near the configured home never appears in any
export output.

---

# Wave 3 — Clustering, selection, timeline

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| T24 | Event detection (M6) | done | claude (main) | T20, T22 |
| T23 | Near-dup clustering (M7) | done | claude (main) | T14, T24 |
| T30 | Selection (M10) | done | claude (main) | T13, T23 |
| T32 | Overrides file | done | claude (main) | T24, T30 |
| T31 | Timeline & `trip.json` (M12) | done | claude (main) | T24, T25, T30 |

### T24 — Event detection
Split within a day on time gap OR GPS jump from running centroid OR end of a transit segment.
**Must not use landmark labels** — that was the circular dependency in the original draft.
Optional second refinement pass may merge/rename using landmarks after T25.
**Acceptance:** ≥80% precision and recall against the labeled truth set via T18.

### T23 — Near-dup clustering
Two mechanisms, two thresholds: pHash + tight Hamming for exact/near-exact and bursts; CLIP
cosine at a looser threshold for semantically similar shots. **Cluster within an event only.**
Emit `cluster_kind ∈ {exact, burst, similar}`. Nothing is ever deleted.
**Acceptance:** known duplicate fixture pairs cluster; visually distinct fixtures never merge.

### T30 — Selection
Keeper per cluster (score, then resolution, then earliest). Per-event highlights chosen for
quality **and diversity** — greedy max-distance over CLIP embeddings among high scorers, so
you get five different shots rather than five near-identical façades. Day and trip roll-ups.
Unselected media stays tagged and exportable via `--include-all`.
**Acceptance:** ≥70% keeper agreement with the truth set; no obviously blurred or clipped
frame ever ranks first; highlights for a single-subject event are visibly varied.

### T32 — Overrides file
`overrides.toml`: pin/reject specific media, force a keeper, merge or split events, rename an
event or landmark. Applied during `build`. This is the entire Phase 1 editing story.
**Acceptance:** each override type demonstrably changes output, and re-running after an edit
recomputes **no** cached stage.

### T31 — Timeline & `trip.json`
The canonical versioned intermediate artifact both outputs render from: ordered events per
day with local times, place and landmark labels, counts, highlight thumbnails, map points,
transcript excerpts, plus trip aggregates. Version the schema from day one.
**Acceptance:** `trip.json` validates against its schema and contains everything T40 and T41
need — no output reaches back into the DB.

---

# Wave 4 — Outputs & acceptance

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| T33 | Trip context input (new, P02) | done | agent-context | — |
| T40 | Static HTML report (M13) | todo | — | T31 |
| T41 | ChatGPT package (M14) | todo | — | T31, T16 |
| T42 | Non-destructiveness proof | todo | — | Wave 3 |
| T43 | End-to-end acceptance on real trip | todo | — | T40, T41, T42 |

### T40 — Static HTML report
Jinja2 → self-contained dir: trip index, page per day, Leaflet + OSM day path (interpolated
points visually distinct), event cards, gallery. No build step, no server, **read-only, no JS
state.** Pure function of `trip.json` + thumbnails. `story-book report` re-renders from an
existing DB. **Acceptance:** browsable offline via `open index.html`; re-render completes in
under 10 seconds.

### T33 — Trip context input  *(new, from P02)*
**Owns:** `src/story_book/trip_context.py`, `trip_context.example.yaml`, tests

The only input the pipeline cannot extract from media: travellers, journal voice, known plans, and
free-text notes. Optional throughout — the package must be complete without it. Names may be
aliases. Resolved context goes into `trip.json` so both outputs see it, and when it is absent the
prompt must instruct the model to stay factual rather than invent feelings.

**Why it exists:** P02's journal was accurate and impersonal, and said so itself — *"it lacks
personal reactions because none were included in the package."* Highest output-quality gain per
line of code in the project right now. See the plan doc's "Trip context" section.

### T41 — ChatGPT package
**Depends on:** T31, T16, T33

Per day: contact sheets (via T16), `brief.md`, `prompt.md`, and the selected media. **Explicitly
not** a zip of raw photos — ChatGPT cannot do vision on archive contents.

**P02 validated the format and added seven requirements** (full reasoning in the plan doc's
Module 14, "P02 result"):

1. **`manifest.json` is the authoritative artifact**, with a stable `asset_id` (the content hash
   the pipeline already has) mapping to source filename, event, capture time, export path and cell
   ID. Cell IDs are positional and must never be an asset's identity. `brief.md` is *generated
   from* the manifest. `schema_version` from day one.
2. **Video records with explicit negatives** — duration, poster, keyframes, motion score,
   highlight ranges, and `transcript_status` distinguishing `no_speech` (processed, none found)
   from absent (not processed). All already in `video_meta`; the export just omitted it.
3. **Reverse-geocoded place candidates**, not raw coordinates.
4. **Trip context** from T33, or an explicit statement that there is none.
5. **Request structured output** (chapters, captions, layout_pages, video_scenes, uncertainties,
   requested_additional_context) alongside the prose, so editorial decisions can drive a renderer.
6. **Richer per-event location** — centroid, start, end, radius, GPS coverage.
7. **Component quality scores** (sharpness/exposure/contrast/face-when-measured). **Not**
   aesthetic or composition: those are Phase 2 and shipping them would misrepresent what the
   pipeline knows.

Also declare in the manifest whether the package is **preview-only or includes originals** — a
preview cannot support judgements about focus, blink, noise, or crop headroom.

**Acceptance:** pasting `prompt.md` + contact sheets into ChatGPT yields a journal, captions, and a
layout needing only light editing — **met at P02** — *plus* a video storyboard that references
actual footage, which P02 could not produce.

### T42 — Non-destructiveness proof
A test that hashes the entire source tree before and after a full `build` and asserts
byte-identical. Also assert originals are opened read-only and exports are hardlink/copy,
never move. **Acceptance:** the test exists, passes, and runs in CI.

### T43 — End-to-end acceptance on real trip
Walk every numbered success criterion in the plan doc against the real trip. Includes killing
`build` at three different stages and confirming clean resume. **Acceptance:** all 12 criteria
pass, or each failure is logged with a follow-up task.

---

# Pre-flight for Wave 1+ (do these first, they can invalidate the plan)

From Phase 0 in the plan doc. Neither is a coding task; both are cheap and can save weeks.

| ID | Task | Status | Owner |
| --- | --- | --- | --- |
| P01 | Run `profile` on the real trip; retune threshold defaults | done | claude (main) — [findings](./p01_profile_findings.md) |
| P02 | Hand-test the ChatGPT handoff on one real day | **done** | claude + human |
| P03 | Label ~200 photos: event bounds, dup groups, preferred picks | **done** | human — 175 photos, 5 events, 7 dup groups |

P02 is the make-or-break check: if a hand-assembled contact sheet + brief doesn't produce a
journal worth keeping, every upstream module is aimed at the wrong target. Do it before Wave 1
ships.

---

# Cross-task requests

Need a change in a file you don't own? Add a row. The owning agent (or the human) resolves it.

| From | To (task/file) | Request | Status |
| --- | --- | --- | --- |
| T05 | human | `brew install ffmpeg exiftool`, then re-run `tests/fixtures/generate.py`. Blocks T11 and T15. | resolved 2026-07-26 |
| T13 | T14 | T13 needs CLIP zero-shot classification. T14 owns the implementation; T13 defines a narrow interface and mocks it. Agree the signature before either lands. | resolved — interface matched verbatim |
| T12 | T11 | **Correctness bug.** `metadata.py` computes the EXIF offset but never persists it, so `tz_offset_minutes`/`tz_source` stay at defaults and **level 1 of the timezone order can never fire** — every item silently falls through to GPS. One-line fix in `_apply`. | resolved |
| T13 | integrator | `opencv-python-headless` 5.0.0 removed `CascadeClassifier` and ships no cascade data, so face detection never runs and every photo gets the neutral 0.5 face component. **20% of the quality weight is now a constant.** Wire `FaceDetectorYN` + YuNet ONNX, or drop `face` from the weights. | resolved |
| T15 | integrator | No table for poster path / keyframes / fps / motion score. Using a JSON sidecar under `cache_dir` as a stopgap; T40/T41 would have to learn an informal convention. Add a `video_meta` table (bumps `SCHEMA_VERSION`). | resolved |
| T15 | integrator | Needs `VideoConfig.speech_mean_volume_floor_db` (currently a module constant, −50 dB; measured speech ≈ −20.8, silent ≈ −91). | resolved |
| T10 | integrator | Whole-trip stages are cached under `TRIP_SENTINEL`, so a second `build` never re-walks the source tree for newly added files without `--force scan`. Consider an `always_run` flag on `Stage`. | resolved |
| T14 | integrator | `SkipItem` is documented as per-item but is **batch-granular** inside `BatchStage` — raising it for one video would skip every co-batched image. Worked around by filtering in `select()`. Fix the docs or add per-item skip support. | resolved |
| T11 | integrator | Wrote a local `_upsert_device` since `connection.py` has no device helper. Consider consolidating. | resolved |
| T11 | integrator | `profile.py` still has its own shallow copy of the Module 2 field-priority logic. Migrate it onto the new canonical `story_book/exif.py`. | resolved |
| T18 | integrator | `eval.py` is not wired to a CLI command. Entry point: `evaluate_truth_set_file(conn, path)` + `render_report(report)`. | resolved |
| — | integrator | **Place identity was a coordinate cell, not a place.** 159 rows all saying "Vienna" for one trip, because rounding is ~11 m — and `event.place_id` points here, so two events in the same square would fragment. Now find-or-create by resolved content, with orphan cleanup so `--force` matches a fresh build. 159 → 3 rows. | resolved |
| — | integrator | **`should_exclude_from_export` couldn't see the home config**, so it excluded every coordinate-less item even with no home set — silently dropping GPS-less camera/GoPro photos to guard a home that was never configured. Now takes `home`, plus `unknown_location_count()` so the drop is reportable. | resolved |
| — | integrator | **Trip date range used `COALESCE`**, so it was written once and never revised: adding an earlier photo left `start_local` wrong forever. Now recomputed. The test asserting the old behaviour is replaced by one asserting the range extends. | resolved |
| T21 | T41 | `place.country` stores the ISO alpha-2 code (`AT`), not a full name. P02's brief wants "Austria". Mapping is a presentation concern — T41's job, noted so it isn't forgotten. | open — T41 |
| P02 | T41 | **Format validated; seven additions required.** Manifest with stable asset IDs, video records with explicit `no_speech`, geocoded place candidates, trip context, structured output request, richer event location, component scores. See the T41 entry. | open — T41 |
| P02 | T13/T30 | Content taxonomy should not be binary keep/reject — a ticket or menu is a scrapbook element, not trash. Four-way (`exclude`/`archive-only`/`scrapbook-candidate`/`story-evidence`) **deferred to Phase 2**; Phase 1's job is only keeping screenshots out of highlights. | deferred |
| P02 | T24 | **Module 6's centroid rule amended** after 129 items over 8¾ hours became one event. | resolved — but see the correction below |
| T24 | plan doc | **The P02 diagnosis was wrong and measurement caught it.** Recent-window comparison makes *no difference* on the real day (6, 12 and 1000 give identical results) and is *worse* on synthetic gradual drift, because it follows you. The entire 4→7 improvement came from `events.max_minutes`. `recent_window` was removed rather than shipped; the plan doc now records the disproof alongside the original claim. | resolved |
| T24 | P03 | Whether `jump_km` should be below 1.5 km needed labels. **Answered: no threshold works.** A grid search over gap/jump/duration tops out at F1 57%. | resolved — see below |
| P03 | T12 | **Nine-hour timestamp bug found by hand-labelling.** Conflict resolution discarded the EXIF offset and read the wall time as GPS-local; the tag is the best evidence for the *instant*, GPS for the *zone*. Two photos sat 9h from their own filename neighbours. Fixed; labels are contiguous after it. | resolved |
| P03 | T12 | **The stage overwrote its own input** — it read the raw tag from the same columns it wrote resolved values to, so `--force` silently produced a worse answer than a fresh run. `media.exif_offset_minutes` added (schema v3). | resolved |
| P03 | **human** | **Clusters vs chapters, measured and decided.** Human boundaries sit at 2- and 8-minute gaps and 10–230 m, against within-event movement of 2.8 km — anti-correlated with both time and distance. Grid search tops out at F1 57%; CLIP makes it worse. **Decision: events are internal scoping only** (dedup/selection/landmark sampling), never shown; chapters are proposed by the AI and edited in `overrides.toml`. `max_minutes` removed. | resolved |
| T30 | — | **Day highlights were poor; the disabled face signal was the cause.** Enabling YuNet transformed them: the family at the cathedral, the Golden Hall selfie, the orchestra mid-performance and a laughing child replaced parked vans, museum walls and duplicate façades. Two bugs fixed on the way — detection at native 24 MP found *zero* faces (needs a 1280px working edge), and the component *penalised* background faces below the no-face neutral. | resolved |
| T13 | — | `FACE_FRAC_SATURATION` was 0.15 and unreachable: across a real library the largest face is 0.023 of the frame, so the signal never fired even on the shots it exists to favour. Recalibrated to 0.02. | resolved |
| T13 | **human** | The YuNet model (~230 KB) is fetched, not vendored — `models/` is gitignored and the curl command is in `config.example.toml`. Decide whether to vendor it for a one-command setup. | open — minor |
| T30 | (superseded) | Day highlights were poor because the face signal was disabled. Reviewing them: a hotel doorway from a tram, a street of parked vans, four museum paintings, two near-identical façades — and no cathedral, no Golden Hall, nobody's family. With `models.face_detector_model` unset, `face_count` is NULL for all 277 photos and quality is sharpness+exposure+contrast only, so a flat well-lit façade outscores a portrait. **Configuring a YuNet model is the single highest-value change available.** | OPEN — needs a decision |
| T30 | T13 | Technical quality ≠ narrative importance, as P02's reviewer said. Even with faces enabled, "the concert we came for" is not a measurable image property. Density weighting helps; the rest is chapters and human overrides. | **confirmed by measurement — see P04** |
| P04 | T30/T32 | **The limitation above is now quantified, and it is not a tuning problem.** A review of the 31 day highlights produced 19 labelled decisions (16 add, 3 drop). The quality score does not separate them: adds mean 0.742 (range 0.643–0.799), drops mean 0.713 (range 0.692–0.753), and 7 of 16 adds fall *inside* the drop range. A 3-D sweep over day quota (flat 10, or k·√n for k=1.25–2.5), a minimum time gap (0–45 min) and `diversity_min_distance` (0.15–0.40) recovered at most **7 of 16** adds, and only by growing the book from 31 photos to 61. Two subject-level signals were tested and also fail: face *presence* is **anti**-correlated (18.8% of adds vs a 27.4% base rate), and a CLIP landmark-vs-generic margin puts adds anywhere from the 25th to the 98th percentile against drops at the 27th to 71st. What the 16 adds have in common is visible only by looking: they are one photo each of sixteen *named things*. Conclusion: the gap gets a mechanism (T32), not a threshold. | resolved — T32 |
| T31 | — | **Every database rowid published in `trip.json` churned between builds.** `day`, `event` and `cluster` rows are deleted and re-inserted on each run, so their autoincrement ids climb even when nothing changed: two builds of an identical library produced different documents. Fixed by deriving every published id from the media set — an event is `<date>#<seq>`, a cluster is its keeper's `asset_id`, and the selection records rank and reason with no scope id. Asserted by a test that builds twice and diffs, and verified byte-identical across two builds of the real 286-item trip. | resolved |
| P04 | T30 | `selection.diversity_min_distance = 0.15` is very nearly inert. CLIP cosine between chosen photos runs 0.26–0.84, so the 0.85 gate rejects 4 candidates across 277 photos; at 0.20 it rejects 8. Two near-identical St Stephen's exteriors (cosine 0.82) both reach the book. Now that pins are guaranteed independently of the automatic picks, raising it is a clean decision — but on one trip's data it is still n=1, so it is recorded rather than changed. | open — needs a second trip |
| P03 | T30 | Highlights must spread across the **day**, not per internal cluster — a coarse cluster getting 5 highlights was P02's complaint, and clusters are now deliberately coarse. | open — T30 |
| P03 | T30 | **Photo density is an importance signal, not a boundary signal.** Tested as segmentation (hotspots ≥500 m apart = separate events) and it fails: cross-event hotspot distances run 133–953 m vs 71–586 m within one event — near-total overlap — and revisits to one cell (11:37, 21:15, 22:44) land in different events, so spatial clustering merges them. But density is genuine: 7 cells above average, top cell 28 photos, and those are the places that mattered. Use it to **weight highlight allocation**. | open — T30 |
| T23 | — | **Both defaults were badly wrong, and calibration fixed them.** `phash_max_distance = 6` caught 1 of 9 real duplicate pairs; measured on 11,709 within-event pairs the count runs 19 (≤16), 27 (≤18), 100 (≤20), so 18 sits just below the noise floor — now 100% precision / 89% recall. `similar_min_cosine = 0.92` would have caused a false merge: CLIP cosine **cannot separate** the classes (duplicates 0.836–0.956, distinct 0.838–0.929), so it is raised above what it can resolve and pHash decides. | resolved |
| T23 | — | **Single-linkage clustering was catastrophic on real data** — union-find chained A~B~C into an 18-photo "burst" with internal distances to 40 bits, 11 false pairs per true one. Switched to complete linkage: a photo joins only if it matches *every* member. Precision 45% → 100%. | resolved |
| P03 | T23/T30 | Only 7 duplicate groups labelled. **Decision: build first, measure after** — run on the real trip, show the clusters and keeper picks for review; the 7 groups act as a regression check. | open — T23 |
| P02 | T30 | Selection needs **temporal spread within an event**, not just embedding diversity. On the mega-event it returned five photos from a fifteen-minute span to represent nine hours. Partly a symptom of the event bug, but worth an explicit constraint. | open — T30 |
| P02 | T15 | **Whisper hallucinated on every real clip** — fluent German, Chinese, Greek and Tibetan invented from concert music and street noise. A fabricated quote in a journal is a fabricated memory. Fixed: VAD, language-confidence, avg_logprob and no-speech gates, all configurable; stale rows now deleted on rejection. All 8 bogus transcripts dropped. | resolved |
| — | integrator | **Content classifier was badly miscalibrated.** Bare-word CLIP labels labelled **209 of 277** real travel photos `screenshot` — a *rejected* class — which would have thrown out three quarters of the trip. Fixed with natural-language prompt ensembles: rejected share fell to 6%. | resolved |
| — | integrator | **Sharpness was collapsed to ~0** for every real photo: it divided Laplacian variance by pixel count, so a 12 MP photo scored 0.001 while carrying the largest weight. `overall` had stdev 0.037. Now measured at a fixed 512px short edge against a calibrated reference; sharpness stdev 0.009 → 0.225, overall 0.037 → 0.125. | resolved |
| — | integrator | **Re-scanning wiped all metadata.** `scan` built a fresh `Media` and called the full `upsert_media`, whose `ON CONFLICT` overwrote every column with NULLs. Combined with the new `always_run` this would have emptied the DB on every build while the repopulating stages sat cached. Added `upsert_media_discovery`. | resolved |
| — | integrator | **`timezones` went stale on incremental adds.** Aggregate whole-trip stages need `always_run` too, or a newly added photo keeps a NULL `taken_utc` and vanishes from ordering, days, and the timeline. | resolved |
| — | integrator | **HEIC was never registered in production.** The fixture test called `register_heif_opener()` itself, so the suite proved the library worked while the app never registered it — every HEIC failed. Now registered at package import. `cv2.imread` also cannot read HEIC at all, so image loading moved to Pillow. | resolved |
| — | integrator | **One bad file failed its whole batch.** A single unreadable HEIC failed all 22 co-batched items in embeddings and content-class. Both now try the batch and fall back to per-item. | resolved |
| T25 | integrator | Providers call the API via hand-rolled `urllib` because `pyproject.toml` was off-limits. Consider adding `anthropic` as an optional extra. Also reconsider the `claude-opus-5` default for a few-hundred-image batch naming task. | open — integrator |

# Amendments to the plan doc

Design changes discovered during implementation. Log here, then update the plan doc — never
let code silently diverge.

| Date | Task | Change | Plan doc updated? |
| --- | --- | --- | --- |
| 2026-07-26 | T04 | Added a third stage shape, `BatchStage`, alongside per-item and whole-trip. CLIP and vision APIs are far cheaper in batches, and recording a cache result per item within a batch keeps partial-batch resume correct. An item the batch silently drops is recorded as **failed**, so a provider that quietly skips an image cannot pass as success. | no -- implementation detail below the plan's altitude |
| 2026-07-26 | T03 | Added `phash` and `meta` tables not in the plan's schema list: pHash needs its own store separate from CLIP `embedding`, and `meta` holds `schema_version`. | no |
| 2026-07-26 | P01 | **Timezone fallback order reversed.** `OffsetTimeOriginal` no longer wins unconditionally: when it disagrees with the offset implied by the item's own GPS, **GPS wins** and the conflict is reported. Real data had 7 items whose EXIF offset sat 9 hours from the offset their own GPS implies, enough to move them to the adjacent day. **Binding on T12.** | yes — Module 2 |
| 2026-07-26 | P01 | **Video capture time comes from `QuickTime:Keys:CreationDate`,** not `CreateDate`/`MediaCreateDate`, which hold the *export* time on Photos-exported `.mov`. Field priority now differs by media kind, and the source field is recorded so export artifacts can be warned about. **Binding on T11 and T15.** | yes — Module 2 |
| 2026-07-26 | T17 | Offset-crossing counting requires a **sustained run** (3+ consecutive items) rather than any A→B change. On real data 13 interleaved mis-tagged items read as "14 crossings"; the true count is 2. | no — refinement of a metric the plan doesn't specify |

# Log

Newest first. One line per meaningful event: task claimed, task done, surprise found,
decision made.

| Date | Who | Entry |
| --- | --- | --- |
| 2026-07-26 | claude | **P01 done** on a 286-item / 1.9 GB real export. Two bugs and one bad heuristic found, both plan amendments above. Corrected numbers: 4-day span (not 10), largest gap 0.49 days (not 5.88), 2 offset changes (not 14). `config.toml` written locally (gitignored) with `events.gap_minutes = 45` — **half the guessed default of 90**, because this library is shot in dense bursts (p50 gap = 1 min). |
| 2026-07-26 | claude | Fixture set extended for the above: timezone crossing is now 3 items per side (a real crossing is sustained), plus a new `offset_gps_conflict.jpg`. 26 fixtures, 338 tests. |
| 2026-07-26 | claude | **T17 done.** `story-book profile` ships with warnings + a suggested-config table computed from observed data, and `--json`. 308 tests pass. Shared extension allowlist added at `story_book/media_types.py` — **T10 must import it, not fork it**. |
| 2026-07-26 | claude | Retro: `exiftool -fast2` silently zeroed video durations by skipping the moov atom. Flag removed; regression test added. A speed flag that changes how much of a file is read is a correctness flag. |
| 2026-07-26 | claude | **T30 built, in review.** Cluster keepers, event samples, day and trip highlights, with sqrt-weighted per-event allocation so a 129-photo cluster cannot swallow a day. Keeper agreement 67% — but every gap it decides is 0.001–0.02 wide, i.e. noise, so the number is a coin flip rather than a capability. Day highlights are visibly weak because the face signal is disabled. |
| 2026-07-26 | claude | **Third instance of the read-and-write-the-same-field bug, and the worst.** Timezone resolution rewrites `taken_local` and also read from it, re-applying its own correction every run: photos drifted 9h per build until a photo from the 18th was dated the 21st, inventing three days. Fixed the same way as the first instance — `media.exif_local` holds the raw wall reading (schema v4) — plus a five-run idempotence test. |
| 2026-07-26 | claude | **T23 done. Duplicate clustering: precision 100%, recall 89%, F1 94%** against the hand-labelled pairs — zero false merges, one missed duplicate (the loosest pair, at the calibrated boundary). 23 clusters over 47 photos on the real trip. Both config defaults were wrong and are now calibrated on measured distributions rather than guessed. |
| 2026-07-26 | claude | **T24 done.** 20 events across the real trip's 4 days, all 286 items linked; the last day shows Vienna → waypoint → Munich. Measurement disproved the P02 centroid diagnosis before it was built: `recent_window` removed, `max_minutes` is what actually works. |
| 2026-07-26 | claude | **P03 labelling kit generated** at `~/Desktop/p03_labelling_kit` — 175 photos over 2 days, 10 contact sheets with filename/time/gap per cell (videos via poster frames), and a pre-listed but deliberately *un-grouped* `truth_set.toml`. Not pre-filled with the pipeline's guesses, or the eval would flatter itself. |
| 2026-07-26 | claude | **Wave 2 done and integrated.** T20/T21/T22/T26/T33 landed; 11 stages now run end to end. Real trip: 907 tests pass, zero pipeline failures, 280 EXIF + 6 interpolated locations, 3 places (Vienna/Munich/one waypoint — geocoding revealed the trip spans two cities), 4 days. Four integration bugs fixed, three of them the same silent-staleness family. |
| 2026-07-26 | claude | **P02 done — the format is validated.** Real ChatGPT test on one day produced a usable journal, accurate captions, self-flagged uncertainties, and no screenshot/receipt leakage. Seven additions folded into T41; new task T33 (trip context); Module 6 amended; two items deliberately deferred to Phase 2. Wave 2 is now the next build step. |
| 2026-07-26 | claude | **Integration pass done. The pipeline runs end to end.** 286 real items, 1.9 GB, 8m31s, **zero failures**; source verified byte-identical. 753 tests pass. Six bugs found that no unit test could have caught — four of them only visible on the *second* run or on *real* photos. Details in the retro. |
| 2026-07-26 | claude | Pushed to https://github.com/victor-huang/travel-story-book (public). CI runs on macOS + Linux. |
| 2026-07-26 | claude | exiftool 13.55 + ffmpeg 8.1.2 installed; video fixtures generated. **T05 done, 180 tests pass, 0 skips.** T11 and T15 unblocked. Verified exiftool reads the HEIC fixture's `OffsetTimeOriginal` and GPS correctly. |
| 2026-07-26 | claude | **Wave 0 done.** T01-T04, T06 complete; T05 in review (video fixtures need ffmpeg). 178 tests pass. Wave 1 unblocked. |
| 2026-07-26 | claude | Added `BatchStage` beyond the plan's two stage shapes -- CLIP and vision APIs are much cheaper batched, and a per-item cache write inside a batch keeps resume correct. Recorded as an amendment. |
| 2026-07-26 | claude | Resume proven by test: `InterruptingStage` fires a real `SIGINT` after 40 of 100 items; the rerun recomputes exactly the remaining 60. See `tests/backend/test_runner.py::TestResumeAfterInterrupt`. |
| 2026-07-26 | claude | Design note: failed items are *not* cached as complete, so they retry next run. Skipped items *are* complete and never retry. |
| 2026-07-26 | claude | Tracker created. Wave 0 unclaimed; nothing implemented yet. |
