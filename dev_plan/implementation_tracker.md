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
| T10 | Scan & hash (M1) | todo | — | Wave 0 |
| T11 | Metadata extraction (M2) | todo | — | Wave 0 |
| T12 | Timezone resolution (M2) | todo | — | Wave 0 |
| T13 | Quality scoring (M8) | todo | — | Wave 0 |
| T14 | CLIP embeddings (M7) | todo | — | Wave 0 |
| T15 | Video analysis (M9) | todo | — | Wave 0 |
| T16 | Contact sheet renderer (M14) | todo | — | Wave 0 |
| T17 | `profile` command (Phase 0) | done | claude (main) | Wave 0 |
| T18 | Truth set format & eval harness | todo | — | Wave 0 |

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
| T20 | GPS backfill (M3) | todo | — | T11, T12 |
| T21 | Reverse geocoding (M4) | todo | — | T11 |
| T22 | Days (M5) | todo | — | T12 |
| T25 | Landmark provider interface (M11) | todo | — | Wave 0 |
| T26 | Home-location privacy filter | todo | — | T21 |

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
exports by default. **Acceptance:** a fixture near the configured home never appears in any
export output.

---

# Wave 3 — Clustering, selection, timeline

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| T24 | Event detection (M6) | todo | — | T20, T22 |
| T23 | Near-dup clustering (M7) | todo | — | T14, T24 |
| T30 | Selection (M10) | todo | — | T13, T23 |
| T32 | Overrides file | todo | — | T24, T30 |
| T31 | Timeline & `trip.json` (M12) | todo | — | T24, T25, T30 |

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

### T41 — ChatGPT package
Per day: contact sheets (via T16), `brief.md` (timeline, events, landmarks, transcript
excerpts, counts, contact-sheet index mapping), `prompt.md` (ready to paste), `full/`
(full-res selected media). Plus a trip-level overview package.
**Explicitly not** a zip of raw photos — ChatGPT can't do vision on archive contents and chat
attachment limits are far below a day's photo count. See Module 14 for the reasoning.
**Acceptance:** pasting `prompt.md` + contact sheets into ChatGPT yields a journal, captions,
and a layout needing only light editing. **A human must verify this one.**

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
| P02 | Hand-test the ChatGPT handoff on one real day | todo | — |
| P03 | Label ~200 photos: event bounds, dup groups, preferred picks | todo | — |

P02 is the make-or-break check: if a hand-assembled contact sheet + brief doesn't produce a
journal worth keeping, every upstream module is aimed at the wrong target. Do it before Wave 1
ships.

---

# Cross-task requests

Need a change in a file you don't own? Add a row. The owning agent (or the human) resolves it.

| From | To (task/file) | Request | Status |
| --- | --- | --- | --- |
| T05 | human | `brew install ffmpeg exiftool`, then re-run `tests/fixtures/generate.py`. Blocks T11 and T15. | resolved 2026-07-26 |
| T13 | T14 | T13 needs CLIP zero-shot classification. T14 owns the implementation; T13 defines a narrow interface and mocks it. Agree the signature before either lands. | open |

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
| 2026-07-26 | claude | Pushed to https://github.com/victor-huang/travel-story-book (public). CI runs on macOS + Linux. |
| 2026-07-26 | claude | exiftool 13.55 + ffmpeg 8.1.2 installed; video fixtures generated. **T05 done, 180 tests pass, 0 skips.** T11 and T15 unblocked. Verified exiftool reads the HEIC fixture's `OffsetTimeOriginal` and GPS correctly. |
| 2026-07-26 | claude | **Wave 0 done.** T01-T04, T06 complete; T05 in review (video fixtures need ffmpeg). 178 tests pass. Wave 1 unblocked. |
| 2026-07-26 | claude | Added `BatchStage` beyond the plan's two stage shapes -- CLIP and vision APIs are much cheaper batched, and a per-item cache write inside a batch keeps resume correct. Recorded as an amendment. |
| 2026-07-26 | claude | Resume proven by test: `InterruptingStage` fires a real `SIGINT` after 40 of 100 items; the rerun recomputes exactly the remaining 60. See `tests/backend/test_runner.py::TestResumeAfterInterrupt`. |
| 2026-07-26 | claude | Design note: failed items are *not* cached as complete, so they retry next run. Skipped items *are* complete and never retry. |
| 2026-07-26 | claude | Tracker created. Wave 0 unclaimed; nothing implemented yet. |
