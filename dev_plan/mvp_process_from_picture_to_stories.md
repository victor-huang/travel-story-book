# MVP: From Pictures to Stories

> This doc is **what and why** — the source of truth for design.
> [`implementation_tracker.md`](./implementation_tracker.md) is **who, in what order, and how
> not to collide** — the source of truth for progress and parallel-agent coordination.
> Amend this doc rather than letting code diverge from it.

## Vision

Turn thousands of unsorted travel photos and videos into an organized, deduplicated,
chronological trip story with almost no manual work — and produce a package a human can
hand to ChatGPT to get a travel journal, captions, and a photo-book layout back.

## The one-sentence MVP

> Point the CLI at **one trip's** folder of 8,000 mixed photos and videos. Get back an
> organized export directory, a browsable static HTML timeline + map, and a per-day ChatGPT
> upload package.

## Decisions (locked)

| Decision | Choice | Why |
| --- | --- | --- |
| Phase 1 deliverable | Python CLI + static HTML report | No Electron/FastAPI/React. A desktop app is ~half the budget and delivers none of the core value. A generated static site gives 80% of the UI in ~2 days. |
| Story generation | Manual ChatGPT upload package | Uses the existing ChatGPT subscription; no API key or per-trip cost. Constrains export format — see Module 14. |
| Compute | Local models; cloud only for landmark recognition | pHash, CLIP, quality scoring, and Whisper run on-machine. Only landmark naming hits a hosted vision API, and only on representatives. |
| Dev data | A real full trip, plus a hand-labeled truth set | Thresholds cannot be tuned by intuition. See Phase 0. |
| Runtime target | **None.** Overnight is fine; resumable is mandatory | Removes all pressure to compromise quality for speed — full-size CLIP batches, larger Whisper models, no sampling shortcuts. Makes constraint #2 below the load-bearing requirement instead. |
| Scope per run | **One trip per run and per DB** | The input folder *is* the trip. Eliminates trip-boundary detection entirely and keeps every query trivially scoped. |
| Report editing | **None. Regenerate instead** | No write-back from HTML, no server, no state in the browser. Corrections are made in an overrides file and the report is re-rendered — cheap, because of stage caching. |

## Out of scope for Phase 1

Cloud sync · desktop/mobile app · photo or video editing · face clustering and naming ·
printing/photo-book vendor integration · social sharing · automated story generation ·
narrated or cinematic video rendering.

---

# Non-negotiable constraints

These shape the architecture and must hold from the first commit.

### 1. Non-destructive, always

The tool **never** modifies, moves, or deletes anything in the source directory. It opens
originals read-only. All output goes to a separate `--out` directory and a SQLite DB.
"Deduplication" means *selecting* a keeper, never deleting a file. Exports are hardlinks or
copies, never moves.

### 2. Resumable and idempotent

There is no runtime target, which makes this constraint the one that matters. A multi-hour
pipeline over 8,000 files that shells out to ffmpeg and calls a network API *will* be
interrupted — closed laptop, OOM, expired API key, ctrl-C. Design for it up front;
retrofitting this is a rewrite.

- Every media item is identified by a content hash (BLAKE2b of file bytes), not its path.
  Re-importing the same photo from a different folder is a no-op.
- Every stage result is cached in a `stage_result` table keyed by `(media_hash, stage,
  stage_version)`. Re-running the pipeline recomputes only what is missing or stale.
- Bumping a stage's `stage_version` constant invalidates exactly that stage.
- `--dry-run` reports what would be computed and roughly what it would cost.

### 3. Privacy by default

- No telemetry, ever.
- `home_location` + `home_exclusion_km` in config: media within that radius is flagged and
  excluded from all exports by default. Leaking your home address into a shared album is
  the failure mode that matters here.
- Nothing leaves the machine except the images explicitly sent for landmark recognition,
  and that stage is skippable with `--no-cloud`.

### 4. Cost and time are budgeted, not discovered

Landmark recognition runs on **event representatives only** (~1 per near-duplicate cluster
per event), not per photo. On a realistic trip this is a few hundred calls, not 8,000. The
pipeline prints an estimated call count and cost before the cloud stage and requires
confirmation above a configurable threshold.

---

# Corrected pipeline order

The original draft had two circular dependencies: event detection consumed landmark labels
that were produced three modules later, and deduplication picked a "keeper" before quality
scores existed. Correct order:

```
                    source folders (read-only)
                              │
                    ┌─────────▼─────────┐
                    │ 1. Scan & hash    │
                    └─────────┬─────────┘
                    ┌─────────▼─────────┐
                    │ 2. Metadata       │  EXIF, GPS, timezone resolution
                    └─────────┬─────────┘
                    ┌─────────▼─────────┐
                    │ 3. GPS backfill   │  interpolate location for GPS-less media
                    └─────────┬─────────┘
                    ┌─────────▼─────────┐
                    │ 4. Reverse geocode│  coords → place names (cached)
                    └─────────┬─────────┘
                    ┌─────────▼─────────┐
                    │ 5. Days           │  group into local calendar days
                    └─────────┬─────────┘
                    ┌─────────▼─────────┐
                    │ 6. Events         │  time gap + GPS jump, within a day
                    └─────────┬─────────┘
              ┌───────────────┼───────────────┐
    ┌─────────▼───────┐ ┌─────▼─────┐ ┌───────▼────────┐
    │ 7. Near-dup     │ │ 8. Quality│ │ 9. Video       │
    │    clustering   │ │    scoring│ │    analysis    │
    └─────────┬───────┘ └─────┬─────┘ └───────┬────────┘
              └───────────────┼───────────────┘
                    ┌─────────▼─────────┐
                    │ 10. Selection     │  keeper per cluster; highlights per event
                    └─────────┬─────────┘
                    ┌─────────▼─────────┐
                    │ 11. Landmarks     │  cloud vision, representatives only
                    └─────────┬─────────┘
                    ┌─────────▼─────────┐
                    │ 12. Timeline      │  narrative structure per day
                    └─────────┬─────────┘
              ┌───────────────┴───────────────┐
    ┌─────────▼─────────┐         ┌───────────▼───────────┐
    │ 13. HTML report   │         │ 14. ChatGPT package   │
    └───────────────────┘         └───────────────────────┘
```

Note stages 7–9 are mutually independent and parallelizable, and that landmark
recognition (11) runs *after* selection (10) precisely so it only sees a few hundred
images. Landmark labels then feed event *naming* and optional event *refinement* on a
second pass — they are not an input to the initial split.

---

# Module specs

Each module lists its acceptance criteria. A module isn't done until its criterion is
demonstrable on the real trip data.

## 1. Scan & hash

Walk the source tree. Supported: JPG, JPEG, PNG, HEIC, HEIF, TIFF, DNG, WEBP (images);
MOV, MP4, M4V, AVI (video). Record path, size, mtime, BLAKE2b content hash.

HEIC decoding is the first real portability trap. Use `pillow-heif`, and add a smoke test
that decodes a committed HEIC fixture — this is the dependency most likely to break on a
fresh machine.

**Acceptance:** scanning the real trip twice produces zero new rows the second time.

## 2. Metadata extraction

Via ExifTool (`pyexiftool`, batched — one process for the whole run, not one per file;
per-file process spawn is the difference between 2 minutes and 40).

Extract: `DateTimeOriginal`, `OffsetTimeOriginal`, GPS lat/lon/altitude, make, model, lens,
orientation, dimensions, duration, `SubSecTimeOriginal`.

### Timezones — the highest-risk detail in the whole project

`DateTimeOriginal` is a naive local timestamp with no zone. On a trip that crosses zones
(and on any trip where a camera's clock was never set to local time), this silently
corrupts the primary organizing axis: day boundaries land in the wrong place and photos
from two devices interleave incorrectly.

Resolution order per item — **revised after P01 against real data**:

1. `OffsetTimeOriginal`, **but only if it agrees with the offset implied by the item's own GPS
   coordinates.**
2. Timezone from GPS via `timezonefinder` (offline). **This wins any disagreement with the EXIF
   offset**, and the conflict is recorded and reported.
3. Timezone inferred from the nearest-in-time GPS-bearing item on the same device.
4. Trip default timezone from config.

The original draft trusted `OffsetTimeOriginal` unconditionally at step 1. P01 on a real
286-item export found **7 items whose EXIF offset was 9 hours away from the offset their own GPS
implies** — enough to land them on the adjacent day. An edited or re-exported photo can carry the
*editing machine's* offset rather than the camera's, so the tag is a hint, not ground truth. GPS
is physical evidence and must outrank it. See
[`p01_profile_findings.md`](./p01_profile_findings.md).

### Video capture time is in a different field

Also from P01: on Photos-exported `.mov`, `QuickTime:CreateDate`, `MediaCreateDate`, and every
`Track*CreateDate` hold the **export** time, not the capture time. Only
`QuickTime:Keys:CreationDate` holds the real capture time — and it carries the original UTC
offset. Reading the conventional field put all 9 clips on the day they were exported, inventing a
phantom fifth day and a 5.9-day gap in a 4-day trip.

Field priority must therefore differ by kind:
- **video:** `Keys:CreationDate` → `DateTimeOriginal` → `CreateDate` → `MediaCreateDate`
- **image:** `DateTimeOriginal` → `CreationDate` → `CreateDate` → `MediaCreateDate`

Record which field supplied the timestamp, and warn when anything falls back to
`CreateDate`/`MediaCreateDate` on a video, since that value is probably an export artifact.

Store both the naive local time *and* a resolved UTC instant. Order by UTC; display and
split days by local time. Provide a per-device clock-offset override in config
(`devices."Sony ILCE-7M4".clock_offset_minutes`) for cameras with a wrong clock — detect
candidates by comparing a device's timestamps against GPS-bearing items and warn loudly.

**Acceptance:** on a day where the trip crosses a timezone boundary, every item lands on
the correct local calendar day and cross-device ordering matches reality.

## 3. GPS backfill

The source list includes a Sony camera and a GoPro — neither records GPS; the phone does.
Without backfill, GPS-based event splitting and the map only work for a fraction of the
library.

For each GPS-less item, interpolate from the nearest GPS-bearing items before and after it
in time (any device). Record `gps_source ∈ {exif, interpolated, manual, none}` and an
estimated confidence; refuse to interpolate across gaps larger than a configurable window
(default 2 hours). Never present interpolated coordinates as exact.

**Acceptance:** ≥90% of GPS-less trip media receives a plausible interpolated location, and
the HTML map visually distinguishes interpolated points from measured ones.

## 4. Reverse geocoding

Coordinates → (place, city, region, country). Offline-first via a bundled
GeoNames-derived dataset for cities/regions, so the common case needs no network and no
rate limit. Optional Nominatim lookup for POI-level names, aggressively cached in SQLite
keyed by coordinates rounded to ~50m, and rate-limited to comply with its usage policy.

**Acceptance:** every event gets at least a city-level label with zero network calls.

## 5. Days

One trip per run, so there is **no trip-boundary detection** — the input folder is the trip.
Trip name and dates come from config or are derived from the media's own date range. This
deletes a whole class of heuristic and its failure modes.

Group into local calendar days with a configurable day-start hour (default 04:00, so a late
night belongs to the evening it started rather than becoming a one-item 1am "day").

Warn — don't split — if the media contains a gap larger than `suspicious_gap_days`
(default 3), since that usually means two trips were passed in one folder. The user decides
what to do about it.

## 6. Event detection

Within a day, start a new event when **any** of:
- time gap > `event_gap_minutes`, or
- distance from the **recent** items' centroid > `event_jump_km`, or
- the event has run longer than `event_max_minutes` (a coarse backstop), or
- sustained movement (a transit segment) ends.

**Revised after P02 — the original "running cluster centroid" rule does not work.** As an event
accumulates items its centroid converges on the average of everything so far, so individual
1–2 km moves stop exceeding the threshold and the event never ends. On one real day this collapsed
**129 items spanning 11:31–20:15** — an entire afternoon across several places — into a single
event, which in turn made selection represent nine hours with five photos taken within fifteen
minutes of each other.

Compare against a **recent window** (the last handful of located items) rather than the whole
event, so walking away from where you just were still registers. The maximum-duration backstop
covers photographing continuously without moving far. With both, the same day split into 7 events
with plausible spans. Binding on T24.

Deliberately **not** using landmark labels here — that was the circular dependency. A
second refinement pass after Module 11 may merge or rename events using landmark labels.

Both thresholds are config-tunable, and the truth set from Phase 0 is what tunes them.

**Acceptance:** on the labeled day set, event boundaries match hand-labeled boundaries with
≥80% precision and recall (a merged pair counts as one miss).

## 7. Near-duplicate clustering

Two distinct problems, two mechanisms — the original draft conflated them:

- **pHash (+ tight Hamming threshold)** catches exact and near-exact duplicates: the same
  photo imported twice, a Google Photos export overlapping an iPhone export, burst frames.
- **CLIP embedding cosine similarity (looser threshold)** catches semantically similar
  shots: eleven photos of the same castle from slightly different angles.

Cluster within an event only (never across events — that's what made naive global
clustering merge unrelated scenes). Emit `cluster_id` + `cluster_kind ∈ {exact, burst,
similar}`. No file is deleted; clusters are metadata.

CLIP runs locally (`open_clip`, ViT-B/32 is plenty), batched, on MPS where available.
Embeddings are cached by content hash — they are the most expensive local computation and
must survive a re-run.

**Acceptance:** on the labeled set, known duplicate pairs cluster together with no
false merges of visually distinct subjects.

## 8. Quality scoring

Split into two tiers, because the draft's single list mixed trivial and hard work.

**Phase 1 (cheap, deterministic, OpenCV):**
- sharpness — variance of Laplacian, normalized by resolution
- exposure — histogram clipping at both ends
- contrast / dynamic range
- a face-presence signal: face count and largest-face fraction of frame
- a "is this actually a travel photo" classifier: CLIP zero-shot against prompts for
  screenshot / receipt / document / parking sign / food / landscape / group photo.
  Filtering out screenshots and receipts is one of the highest-value cheap wins.

**Deferred to Phase 2:** smile detection, eyes-open, "people centered," any learned
aesthetic/composition model. Each is a separate model with its own failure modes and is not
required for a usable first release.

Overall score is an explicit, documented weighted sum of the Phase 1 signals — no opaque
magic number.

**Acceptance:** on the labeled set, the top-scoring photo in a cluster matches the
human-preferred photo ≥70% of the time; the tool never ranks an obviously blurred or
clipped frame first.

## 9. Video analysis

Per video, via FFmpeg: duration, resolution, fps, a poster thumbnail, evenly-spaced key
frames, and a motion/scene-change score for highlight candidates.

Speech transcription via local `faster-whisper`. With no runtime target, this stage can be
generous: default to `--transcribe auto` (clips longer than `transcribe_min_seconds`,
default 10, that carry a speech-likelihood signal) with `all` and `none` also available, and
default to the `small` model rather than `base` since an extra hour overnight costs nothing.

Because transcription is the slowest per-item stage, it must checkpoint per video, not per
run — an interrupt should never lose more than one clip's work.

**Acceptance:** every video has a thumbnail and duration; `auto` mode transcribes the clips
that actually contain narration and skips silent b-roll; interrupting mid-stage and
re-running re-transcribes at most one clip.

## 10. Selection

- One **keeper** per near-duplicate cluster (highest overall score, tie-broken by
  resolution then earliest timestamp).
- Per event, N **highlights** (default 5) chosen for quality *and diversity* — greedily
  maximize CLIP-embedding distance among high scorers so you get five different shots of
  the castle grounds rather than five near-identical façades.
- Per day and per trip, a "best of" roll-up drawn from event highlights.
- Everything not selected stays in the DB, tagged, and is exportable with
  `--include-all` — the user must always be able to overrule the algorithm.

### Corrections without an editable UI

Since the report is read-only, human corrections live in a hand-edited
`overrides.toml` next to the config — pin or reject specific photos, force a keeper, merge
or split events, rename an event or landmark. `build` reads it, and because every expensive
stage is cached, re-running after an edit takes seconds rather than re-doing the trip.

This is the whole "editing" story for Phase 1: **edit a text file, re-run, refresh the
browser.** It's less pleasant than clicking, but it costs a day instead of three weeks, and
overrides end up version-controllable and diffable.

## 11. Landmark recognition (cloud, representatives only)

**P02 data point worth checking before spending on this.** Given only coordinates and contact-sheet
thumbnails, ChatGPT correctly named the Hofburg, St Stephen's Cathedral, the Vienna State Opera and
the Musikverein. It may have inferred those from the images rather than the coordinates, and it
cautioned against relying on the behaviour — but it suggests the marginal value of a dedicated
landmark pass may be smaller than assumed *for famous landmarks in a well-fed package*. Measure
that before committing to per-trip API spend: the honest comparison is a package with landmark
labels against one without, judged on the journal.


Send only event highlights plus cluster keepers — a few hundred images per trip. Batch
multiple images per request with the coordinates and reverse-geocoded place name as
context; that context is what makes the difference between "a palace" and "Belvedere
Palace." Ask for a structured response: landmark name, confidence, one-line description,
and any notable artwork or feature identified.

Provider behind a thin interface with at least two implementations, since this is the one
vendor-coupled stage. Cache every response by content hash + prompt version. `--no-cloud`
skips the stage entirely and the pipeline still produces a complete (just less richly
labeled) result.

**Acceptance:** with `--no-cloud`, the full pipeline still completes and exports. With
cloud enabled, on the labeled set, the majority of well-known landmarks are named
correctly, and the estimated cost printed beforehand is within 20% of actual.

## 12. Timeline builder

Per day: an ordered list of events, each with local start/end time, place label, landmark
labels, item counts, highlight thumbnails, a representative map point, and any video
transcript excerpts. Plus a per-day path for the map and per-trip aggregate stats.

This is the canonical intermediate artifact — a versioned `trip.json` that both outputs
(13 and 14) render from and that a future Phase 2/3 consumes. Version the schema from day
one.

## 13. Static HTML report

A single self-contained output directory: `index.html` for the trip, a page per day,
Leaflet + OpenStreetMap map with the day's path, event cards with thumbnails, and a
gallery. No build step, no server, no JavaScript state — `open index.html`. Rendered from
`trip.json` via Jinja2.

**Strictly read-only and fully regenerable.** The report is a pure function of `trip.json`
plus thumbnails; nothing in it is a source of truth and it can be deleted and rebuilt at any
time. A separate `story-book report` command re-renders from an existing DB in seconds
without touching the pipeline, which makes template iteration fast and makes the
edit-overrides-and-re-run loop practical.

This replaces the desktop app for Phase 1 and is how you'll actually judge whether event
splits and photo picks are any good.

## 14. ChatGPT upload package

**This is the design most changed from the draft, and it's worth understanding why.** The
original plan was "ZIP each day and upload." That does not work: ChatGPT does not perform
vision analysis on images buried inside an archive, and chat attachment limits are far
below 124 images. A per-day zip of raw photos produces a journal written from filenames.

What works in the chat UI, with the images actually visible:

Per day, emit:
1. **Contact sheets** — a small number of montage JPEGs, each a labeled grid of ~12–20
   highlight thumbnails with an index caption per cell (`03-07: 11:45 Hohensalzburg
   Fortress`). Two or three images per day, which ChatGPT can genuinely look at.
2. **`brief.md`** — a compact human- and model-readable day brief: timeline, event list with
   times and place names, landmark labels, video transcript excerpts, counts, and the
   contact-sheet index mapping so the model can reference specific photos by ID.
3. **`prompt.md`** — a ready-to-paste instruction asking for the travel journal, captions
   keyed to photo IDs, a photo-book layout, and a video storyboard.
4. **`full/`** — the full-resolution selected media, for when the user wants to attach a
   handful of specific shots the model called out.

Also emit a per-trip package with one contact sheet per day for an overview pass.

**Acceptance — and this is the make-or-break test:** run this by hand on one real day
*in week one*, before building any upstream module. If pasting `prompt.md` plus the contact
sheets into ChatGPT does not produce a journal you'd actually keep, every upstream module is
optimizing toward the wrong target and the handoff design needs to change first.

### P02 result: the format passed, with seven required additions

Tested on a real 141-item day. The journal draft was chronologically accurate and usable, the
captions matched what is visible, the model flagged its own uncertainties rather than inventing,
and no screenshot or receipt reached the 29 selected photos. **The format is validated.** The
contact-sheet + brief + prompt shape stays.

It also identified the Hofburg, St Stephen's Cathedral, the State Opera and the Musikverein
correctly from coordinates and images alone — a useful data point for how much Module 11 has to
carry (see the note there).

Seven additions are now part of Module 14's spec, in priority order:

1. **`manifest.json`, and it is the authoritative artifact.** Contact-sheet cell IDs are
   *positional* — they change whenever selection changes — so they cannot be the identity of an
   asset. The pipeline already has a stable identity (the BLAKE2b content hash); the package
   simply never exposed it. Emit a manifest mapping a stable `asset_id` to source filename,
   content hash, event, capture time, export path, and cell ID. `brief.md` is **generated from
   the manifest**, not maintained alongside it. Carry a `schema_version` from the first release.
2. **Video records, including explicit negatives.** Videos were counted in the brief but
   otherwise absent — no duration, poster, or transcript — so the requested storyboard could
   only be invented from stills. Every video needs a record: duration, poster frame reference,
   keyframes, motion score, and highlight ranges. Crucially, `transcript_status` must
   distinguish **`no_speech`** (processed, nothing found) from **absent** (not processed). All
   of this already exists in `video_meta`; only the export omitted it.
3. **Reverse-geocoded place candidates, not raw coordinates.** Asking the model to resolve
   coordinates is fragile — it may name a place from the image rather than the position, and be
   confidently wrong. Ship `country`/`city`/`neighborhood` plus candidate places with distances
   and confidences. Confirms the priority of Modules 4 and 11.
4. **Trip context.** The prompt asks for a first-person journal, but the package says nothing
   about who travelled, whose voice is speaking, what was planned, or what mattered. The draft
   was accurate and impersonal for exactly that reason: *"it lacks personal reactions because
   none were included in the package."* See the new section below.
5. **Structured output alongside prose.** Ask for a JSON block (chapters, captions,
   layout_pages, video_scenes, uncertainties, requested_additional_context) in addition to the
   readable answer. Without it the model's editorial decisions are trapped in prose and cannot
   drive a book or video renderer.
6. **Richer per-event location.** One averaged coordinate can place an event somewhere nobody
   stopped, and hides movement. Emit centroid, start, end, radius, and GPS coverage; for walking
   events, a simplified path.
7. **Component quality scores, not just `overall`.** A bare `0.88` tells the model nothing about
   *why* a photo won. Ship the components that actually exist — sharpness, exposure, contrast,
   and face when measured. **Not** aesthetic or composition scores: those are explicitly Phase 2
   and inventing them here would misrepresent what the pipeline knows.

Also state in the manifest whether the package is **preview-only or includes originals**. A
preview package cannot support judgements about focus, blink, noise, or crop headroom, and the
recipient should be told rather than left to infer.

### Clusters are not chapters

P02 named this precisely: what Module 6 produces is a **time-and-location cluster**, not a
narrative unit. One real cluster contained a church interior, the State Opera, and an evening
walk — chronologically correct, three different stories.

Resolution, chosen to avoid inventing a semantic-segmentation stage:

- **Clusters stay mechanical** and stay the pipeline's output. Module 6 is unchanged in kind.
- **Chapters are proposed by the model** in its structured output, and approved or edited by the
  human through `overrides.toml`. The package ships clusters; the model suggests how to combine
  or split them into chapters.

This keeps semantics where the judgement actually is, and keeps a wrong guess cheap to fix.

### Deferred to Phase 2, deliberately

P02 proposed a four-way content taxonomy — `exclude` / `archive-only` / `scrapbook-candidate` /
`story-evidence` — on the grounds that a ticket, menu, or receipt can be a legitimate scrapbook
element rather than trash. The principle is right and the binary keep/reject is crude. But
Phase 1's job is narrower: keep screenshots and receipts *out of highlights*. The taxonomy
expands Module 8's label set and Module 10's logic, so it waits.

Likewise the distinction between **technical quality** and **emotional or narrative importance**
is real and important — a technically weaker family photo often matters more than a perfect
façade — but scoring it needs either face identity (Phase 2) or human input, so Phase 1 ships
technical quality only and says so.

---

# Trip context — the one input the pipeline cannot extract

Added after P02. Every other input to this system is derived from the media. This one cannot be,
and its absence was the single clearest weakness in the generated journal, which said of itself:
*"This draft is chronologically grounded, but it lacks personal reactions because none were
included in the package."*

A photo library records **what was photographed**. It does not record who was there, whose voice
the journal should be in, what was planned versus stumbled upon, or what anyone felt. No amount of
better vision models recovers that — it has to be supplied.

So Phase 1 accepts a small, entirely optional context file, read into the package:

```yaml
travelers:
  - role: narrator
    name: <optional, alias allowed>
  - role: spouse
  - role: child
    name: <optional>
journal_voice: first_person_plural      # or first_person_singular
known_plans:
  - "Attend the Mozart concert at the Musikverein, 20:15"
notes:
  - "The concert was one of the main reasons for coming to Vienna."
```

Design constraints:

- **Optional throughout.** The pipeline must produce a complete package with no context file. It
  simply produces a more impersonal journal, and the manifest says so.
- **Names may be aliases.** Real names are the user's choice, not a requirement, and the project's
  privacy stance means the tool should never push for them.
- **Free-text notes are the highest-value field per keystroke.** One line about why a day mattered
  changes the journal more than any additional extracted metadata. The package should ask for
  little and use whatever it gets.
- **Never invent it.** If context is absent, the prompt must tell the model to stay factual rather
  than manufacture feelings. An invented emotion is the same failure as an invented quote.

Store the resolved context in `trip.json` so both outputs see it.

---

# Data model (SQLite)

One DB per trip (`<out>/story.db`), so the `trip` table holds exactly one row and no query
needs a trip filter. Keeping the table rather than inlining the fields costs nothing and
leaves the door open to a multi-trip Phase 2 without a migration.

One `media` table with a `kind` column, not parallel `photo`/`video` tables — otherwise
every downstream stage doubles. `person` is dropped from Phase 1 (face clustering is
explicitly Phase 2; an unused table invites half-implementations).

```
media          hash PK, path, kind, bytes, mtime, width, height, duration,
               device_id, taken_local, taken_utc, tz_source, lat, lon,
               gps_source, gps_confidence, place_id, is_near_home
device         id PK, make, model, clock_offset_minutes
place          id PK, lat_rounded, lon_rounded, poi, city, region, country, source
trip           id PK, name, start_local, end_local, home_lat, home_lon   -- one row
day            id PK, trip_id FK, local_date
event          id PK, day_id FK, seq, start_utc, end_utc, centroid_lat,
               centroid_lon, place_id, label
media_event    media_hash FK, event_id FK
cluster        id PK, event_id FK, kind, keeper_hash
media_cluster  media_hash FK, cluster_id FK
score          media_hash PK, sharpness, exposure, contrast, face_count,
               face_max_frac, content_class, overall
embedding      media_hash PK, model, vector BLOB
landmark       id PK, name, confidence, description, source, prompt_version
media_landmark media_hash FK, landmark_id FK
transcript     media_hash PK, model, text, segments JSON
selection      media_hash FK, scope, scope_id, rank, reason
stage_result   media_hash, stage, stage_version, status, error, computed_at  PK(1,2)
```

---

# Tech stack

| Component | Technology |
| --- | --- |
| Language | Python 3.12, `uv` for env and deps |
| CLI | Typer + Rich (progress, since runs are long) |
| Database | SQLite via stdlib `sqlite3`, WAL mode |
| Metadata | ExifTool via `pyexiftool`, batched single process |
| HEIC | `pillow-heif` |
| Images | Pillow + OpenCV |
| Embeddings | `open_clip` ViT-B/32, local, MPS-accelerated |
| Video | FFmpeg via subprocess |
| Transcription | `faster-whisper`, local |
| Timezones | `timezonefinder` (offline) |
| Geocoding | bundled GeoNames extract; optional Nominatim |
| Landmarks | hosted vision API behind a provider interface |
| Report | Jinja2 + Leaflet + OpenStreetMap |
| Concurrency | `ProcessPoolExecutor` for CPU stages, `asyncio` for network |
| Tests | pytest, with a committed tiny fixture library |

Deliberately absent: Electron, Tauri, React, FastAPI. None are needed for Phase 1.

---

# Phased roadmap

## Phase 0 — Validate the ends before building the middle (week 1)

Do these two things first; both can invalidate the plan cheaply.

1. **Profile the real library.** A throwaway script over the actual trip folder reporting:
   item count by type and device, date range, timezone crossings, % missing GPS, % HEIC,
   total bytes, video count and total duration. Every threshold default above should be
   revisited against these numbers.
2. **Hand-test the ChatGPT handoff** (Module 14 acceptance). Manually assemble one day's
   contact sheet and brief, paste into ChatGPT, judge the output. If this fails, redesign
   the handoff before building anything upstream.

Also in Phase 0: build the **truth set** — hand-label ~200 photos from 3–4 days with
event boundaries, duplicate groups, and preferred pick per group. This is what makes
Modules 6, 7, 8, and 10 tunable rather than guesswork, and it's the single most useful
half-day in the project.

## Phase 1 — The organization engine (weeks 2–7)

Ships the CLI, the pipeline (Modules 1–12), the static HTML report, and the ChatGPT
package. Suggested order, each step ending in something runnable on the real trip:

1. Skeleton: CLI, config, SQLite schema, `stage_result` caching, scan + hash. (Module 1)
2. Metadata + timezone resolution + GPS backfill + geocoding. (2–4)
3. Days, events; first HTML report showing just the timeline and map. **You can now
   look at the real trip's structure** — the most valuable early checkpoint.
4. CLIP embeddings + near-dup clustering. (7)
5. Quality scoring + content classification. (8)
6. Selection with diversity. (10)
7. Video analysis and transcription. (9)
8. Landmark recognition. (11)
9. `trip.json`, full HTML report, ChatGPT package, `overrides.toml`. (12–14)

## Phase 2 — AI generation in-tool

Direct API-based journal, caption, and album-layout generation (replacing the manual
handoff for users who want it); face clustering with names; the deferred aesthetic quality
models; multi-trip library; interactive HTML travel website. An editable UI belongs here
too, if `overrides.toml` turns out to be genuinely annoying in practice — decide with real
usage rather than up front.

## Phase 3 — Documentary

Narrated video assembly, cinematic edits, photo-book vendor export, family collaboration,
receipt/itinerary extraction.

---

# Success criteria for Phase 1

Measured on the real trip, not a toy sample:

1. `story-book build ~/Trips/Europe2026 --out ~/out` completes on the full trip. No wall-clock
   requirement — overnight is acceptable.
2. Killing the process mid-run and re-invoking resumes without recomputing finished work.
   This replaces runtime as the hard performance requirement, and is worth explicitly
   testing (kill at three different stages).
3. The source directory is provably unmodified (compare a full hash manifest before/after).
4. Days and events match hand-labeled boundaries at ≥80% precision and recall.
5. Duplicate groups are correct; the chosen keeper matches human preference ≥70% of the time.
6. Screenshots, receipts, and documents are kept out of highlights.
7. Every trip item appears on the map, with interpolated locations visually distinguished.
8. The HTML report is browsable offline with no server, and `story-book report` re-renders it
   from an existing DB in under 10 seconds.
9. Editing `overrides.toml` and re-running `build` applies the correction without
   recomputing any cached stage.
10. Pasting a day's `prompt.md` + contact sheets into ChatGPT yields a journal, captions, and
    a layout suggestion that need only light editing.
11. Photos near the configured home location are excluded from every export by default.
12. `--no-cloud` produces a complete result with no network calls.

# Open questions

None blocking. The three prior questions are resolved in the decisions table: no runtime
target, one trip per run, read-only regenerable report.

Remaining items are judgment calls best made against real data during Phase 0 — the
threshold defaults (`event_gap_minutes`, `event_jump_km`, pHash and CLIP similarity cutoffs,
highlights-per-event) are all guesses until the labeled truth set exists.

# Repository hygiene (for a public OSS project)

LICENSE (MIT or Apache-2.0), README with a real screenshot of the HTML report, a small
committed fixture set covering HEIC / no-GPS / timezone-crossing / burst / duplicate cases,
pytest suite, GitHub Actions CI on macOS and Linux, `CONTRIBUTING.md`, and a documented
`config.toml` example. The fixture set is what makes the test suite possible at all —
create it during Phase 0 alongside the truth set.
