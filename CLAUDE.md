# CLAUDE.md

Turn one trip's folder of photos and videos into an organized, browsable story plus a package a
human hands to ChatGPT to write the travel journal.

## Read these before starting work

| Doc | Role |
| --- | --- |
| `dev_plan/mvp_process_from_picture_to_stories.md` | **What and why.** Source of truth for design. Amend it rather than letting code diverge. |
| `dev_plan/implementation_tracker.md` | **Who, in what order, and how not to collide.** Source of truth for progress. Claim your task here before writing code. |
| `retro/agentic_coding/` | Errors and lessons from past cycles. Skim the latest before a new one. |

## Commands

```bash
uv sync --extra vision --extra video --extra exif --extra geo   # a bare `uv sync` PRUNES these
uv run pytest                                    # 1700 tests, expect 0 failures 0 skips locally
uv run pytest tests/unit                         # fast, mocked, no DB
uv run story-book init <src> --trip-dir <dir> [--like <other/config.toml>]  # scaffold a trip
uv run story-book build <src> --out <dir>        # the pipeline
uv run story-book report --out <dir> [--story story.json] [--context ctx.yaml]  # re-render
uv run story-book profile <src>                  # folder stats + suggested config
uv run story-book package --out <dir> [--zip] [--video-proxies] [--max-part-mb 200]  # ChatGPT package
uv run story-book reel --out <dir> [--music f] [--aspect 9:16] [--day D]  # video montage
uv run story-book check-story <story.json> --out <dir>   # validate the model's answer
uv run story-book eval <truth.toml> --out <dir>   # score against a labelled truth set
uv run python tests/fixtures/generate.py         # regenerate fixtures (deterministic)
```

Before committing: `uv run autoflake -i --remove-all-unused-imports $(find src tests -name '*.py')`
then `uv run ruff check . && uv run ruff format . && uv run pytest`.

**A green local suite is enough — do not block on CI.** Push and move on. CI runs the same tests on
macOS and Linux, so it earns a wait only for a large or risky change, and then in the background.
Report the CI result only when it fails.

System binaries: `exiftool` (metadata) and `ffmpeg` (video). Both installed here.

## Non-negotiable guarantees

Break these and the tool is worthless regardless of features.

1. **Non-destructive.** The source tree is opened read-only and never modified, moved, or
   deleted. Deduplication *selects a keeper*; it never removes a file. Output goes only to
   `--out` and the DB. Exports hardlink or copy, never move.
2. **Resumable.** There is no runtime target — overnight is fine — so resumability is *the*
   performance requirement. Every stage result is cached by content hash and committed per item.
   An interrupt loses at most one item's work. Proven by test, not assumed:
   `tests/backend/test_runner.py::TestResumeAfterInterrupt` fires a real `SIGINT`.
3. **Private.** No telemetry. Media within `home_exclusion_km` of the configured home is kept
   out of every export by default. `--no-cloud` completes the whole pipeline with zero network
   calls, and every stage must degrade rather than abort when unavailable.

## Architecture

One SQLite DB per trip in `--out/story.db`; the `trip` table holds exactly one row, enforced by
`CHECK (id = 1)`. Media is keyed by BLAKE2b content hash everywhere, never by path — re-importing
the same photo from another folder is a no-op.

Pipeline order (the plan doc's corrected order — note landmarks run *after* selection so they
only see a few hundred representatives, and event detection must **not** consume landmark labels):

```
scan → metadata → timezones → gps_backfill → geocode → days → events → home_filter
     → (embeddings ∥ quality ∥ video) → phash → dedup → selection → landmarks
     → thumbnails → timeline → (html report ∥ chatgpt package)
```

### Frozen contracts — read, don't change

Changing these breaks every parallel task. Amend only via the tracker's cross-task request table.

- `db/models.py` — the inter-stage data contract. `Media` plus the `StrEnum`s.
- `db/schema.sql` — 19 tables. `stage_result` is the resume backbone.
- `db/connection.py` — use `upsert_media`, `iter_media`, `completed_hashes`,
  `record_stage_result`, `clear_stage`. **Do not write raw SQL against `media` or
  `stage_result`.**
- `pipeline/base.py` — pick a stage shape:
  - `PerItemStage` — per-media work. `compute` is pure and may run in a worker process;
    `persist` runs in the parent and owns the DB. That split exists because a sqlite3
    connection cannot cross a process boundary.
  - `BatchStage` — CLIP, vision APIs. Anything absent from `process_batch`'s returned mapping is
    recorded as **failed**, so a provider that silently drops an image can't pass as success.
  - `WholeTripStage` — aggregate work (days, events, selection, timeline). Cached under
    `TRIP_SENTINEL`.
  - Raise `SkipItem` for "does not apply" (terminal, never retried). **In a `BatchStage` this is
    batch-granular** — it skips every co-batched item — so exclude inapplicable items in
    `select()` instead. Override `available()` to declare a missing binary — never abort the run.
  - `always_run = True` for discovery and whole-media aggregates; see the integration rules below.
- `config.py` — **every threshold lives here. No magic numbers in stage code.** Add a field
  instead.

### `--out` is disposable, except one directory

Everything under `--out` is derived from the source photographs and can be deleted and rebuilt —
except `<out>/story/`, which holds what a chat returned (`story.json`, the trip context, and any
prose beside them). `build` creates it with a README; nothing writes or deletes it. `report` and
`package` read it and wipe only their own directories. Discovery is anchored to `--out`, never the
cwd.

### Published identifiers

`trip.json` is the canonical artifact; the report and the package render only from it and never
read the DB. **Nothing derived from insertion order leaves the database.** `day`, `event` and
`cluster` rows are deleted and rebuilt every run, so their rowids climb even when nothing changed
— publishing one made two builds of an identical library differ. Every published id is a function
of the media set: an asset is a prefix of its content hash, an event is `<date>#<seq>`, a cluster
is its keeper's `asset_id`. Same reason `overrides.toml` addresses everything by filename. A test
builds twice and diffs.

### Cache semantics, exactly

- Key is `(media_hash, stage.name, stage.version)`. **Identity is the `name` string, not the
  class** — a subclass does *not* inherit cache identity.
- `ok` and `skipped` are complete and won't rerun. `failed` is **not** complete and retries next
  run (failures are often transient).
- Bumping a stage's `version` invalidates exactly that stage.

## Conventions

Python 3.12, imports at top of file, inline comments only for genuinely complex logic or edge
cases — not for restating what the code says. Match surrounding style.

### Tests

- Test **classes** per target; **one behavior per test**; prefer `mocker.patch` (pytest-mock).
- `tests/unit/` — no DB, no filesystem, no network. Mock them.
- `tests/backend/` — real temp DB and real fixture media. May create what it needs. (Named for
  consistency with other projects here; means "integration".)
- Every stage needs at least one `tests/backend/` test against a real fixture. Mocked-only
  coverage of EXIF, image, and timezone code is how those bugs escape.
- 27 committed fixtures in `tests/fixtures/media/` cover HEIC+GPS+offset, GPS-without-offset,
  no-GPS (Sony), no-EXIF, burst pair, byte-identical duplicate pair, distinct pair,
  sharp/blurred, screenshot, receipt, over/under exposure, a +02:00→+03:00 timezone crossing,
  speech/silent video clips, an offset-vs-GPS conflict, and a Photos-export-shaped .mov whose
  `CreateDate` disagrees with its `Keys:CreationDate`. Reuse them; regenerate via the
  deterministic generator.
- Fixtures are committed artifacts: **assert their presence, never skip on a proxy** like
  "is ffmpeg installed". Gate on the binary only for checks that truly invoke it
  (`@pytest.mark.needs_ffmpeg`).

### Integration rules learned the hard way

Nine stages each passed their own suite; wiring them together still surfaced six bugs. All six
lived in places a per-stage test cannot reach.

- **Test the seam, not just the sides.** A stage writing a field nobody reads passes every test it
  owns. `tests/backend/test_pipeline_integration.py` is where handoffs get asserted.
- **Run it twice.** Caching means the second `build` takes different paths than the first. Bugs
  found only on run two: re-scan blanking metadata, and aggregate stages going stale.
- **`always_run` is needed by discovery stages and by aggregates over the whole media set**
  (`scan`, `timezones`, and later days/events/selection/timeline). Their cache key is a constant,
  so it encodes no dependency on the media set. Per-item stages never need it.
- **An upsert that writes every column will blank what a partial writer doesn't know about.** Use
  `upsert_media_discovery` from the scanner; own columns, not just tables.
- **If a fixture needs a setup line, ask who runs it in production.** HEIC decoding "worked" for
  months of test-time because the test registered the opener itself and the app never did.
- **Batching trades away fault isolation — pay it back.** Try the batch, fall back to per-item on
  failure, so one bad file costs only itself.
- **Ordering tests don't test calibration.** Sharpness was monotone and correct in order while
  compressed to a 0.001 range, silently neutering the highest-weighted term in the score. Assert
  the *spread* of any component feeding a weighted sum.
- **When writing an export, enumerate the source table's columns and justify each omission.** Two
  independent reviews of the ChatGPT package (P02, P05) found the same defect class: the export
  layer narrowing what the pipeline already knows. A schema test proves the output is well-formed,
  never that it is complete — only a consumer can ask "should this field be here?".
- **Durations and intervals come from `taken_utc`, never local wall time.** `taken_local` carries
  its UTC offset, so mixing it with a naive string raises instead of silently returning a wrong
  interval. This is the standing rule — order by UTC, split days by local — and it took a third
  site to notice it also governs arithmetic.
- **Emit no confidence, score, or measurement the pipeline did not compute.** P05 asked for a place
  `confidence`; the offline geocoder produces none, so the manifest reports `precision: "city"`
  instead. A fabricated number that looks measured is this project's most repeated failure.
- **A test that keeps passing after the thing it tests has changed is worse than one that fails.**
  Two "works offline" tests survived the map becoming Leaflet while asserting nothing true — one
  only looked at the index, the other's regex could not see the tile URL inside a JSON block.
  When a contract changes, reread the tests that claim to guard it.
- **A claim about what is in a photograph is settled by the photograph.** I reported that a
  generated summary had moved a landmark two days, from metadata alone — the traveller pointed out
  they had been there again, and the photograph proved it. Metadata says which file a caption
  points at, never whether the caption is true. `story.json` is still the rendering source of truth,
  because it is the half a renderer consumes and `check-story` can verify.
- **Adding a column that a stage writes means bumping that stage's version.** `media.exif_local`
  landed with `MetadataStage.version` left at 1, so every library built before it kept the column
  NULL forever and the drift it exists to prevent carried on. A fix inside a cached stage reaches
  only data not yet computed — ask what happens to a library that already exists.
- **Publish the target rather than adapt to what arrives.** The story schema and the trip-context
  template both ship inside the package because a model invents its own shape otherwise. An
  adapter guessing at every variation is unbounded work; a published contract is one file.
- **Organising the input silently reorganises the output.** The package is one folder per day and
  its README said "for each day, open a fresh chat" — so three chats each did as told and each
  saved a `story.json` covering one day, which had to be re-asked as one combined file. The day
  split is an *upload size* workaround; the unit of work is the trip. There is now one `prompt.md`
  at the package root asking for exactly one `story.json`, and `--zip` splits on day boundaries
  only when the package exceeds `--max-part-mb`, with every part going to the same conversation.
- **A story is an overlay, never a source of structure.** `--story` adds words to the report; the
  pipeline still decides what exists, and a chapter citing absent media is dropped, not rendered.
- **A published request format ships with a published response format.** Three reviews hardened
  `manifest.schema.json` while the shape the prompt *asks for* was only described in prose — and
  the first real answer renamed three keys and dropped a required one. Both schemas now travel
  inside the package, and `check-story` validates shape and grounding **separately**: the real
  response had 100% of its references resolve and was still unreadable by a renderer.
- **For every declared media type in an export, verify one file's actual bytes.** P06 found nine
  assets declared `kind: "video"` whose exported files were JPEGs under `.mov` names. The schema
  validated, every reference resolved, 87 tests passed — all of them checking *presence*, none
  checking *identity*. `file -b` is two lines.
- **An artifact never overstates its contents.** Where it cannot supply what a consumer would
  assume, it says so at that point: `video_proxies_included`, `transcript_status`,
  `privacy.home_configured`, `asset_scope`. Looking more complete than you are is worse than
  looking incomplete.
- **Never request precision the data cannot support.** Five frames from a 112-second clip cannot
  justify "seconds 43–51", so without proxies the prompt asks for estimates anchored to keyframe
  offsets and flagged in `uncertainties`.
- **When a value gains a dimension, grep every comparison on it.** Adding UTC offsets to
  `taken_local` broke event durations one day and trip bounds the next — same rule (order by UTC,
  split days by local), two sites, found separately.
- **A "no reading" sentinel must not compare cleanly against itself.** `volumedetect` logs at
  info level, so `ffmpeg -v error` silenced it and every measurement returned `-999.0`. The
  assertion happened to be `<`, so it failed; `>=` or an equality would have passed on no
  evidence. Where a measurement can come back empty, assert that it did not.
- **A bundled default is still a design choice.** Pillow's `load_default` font was picked because
  it is always present — and it has no `é ü ö à ñ – —`, so a title card read `July 17□20` and
  every contact sheet would have drawn boxes for a German place name. "Always available" and
  "renders your text" are different properties. Text in images goes through `export/fonts.py`,
  which picks a font by **what has to be drawn** — the first fix transliterated instead, which
  degraded gracefully for accents and *silently deleted* every CJK character. A fallback that is
  graceful for one script can be silent for another; test the scripts it cannot handle.
- **A stage that reads a field a *later* stage writes is not deterministic.** `timezones` runs
  before `gps_backfill`, so on build 1 a GoPro clip had no coordinates and took the
  same-device-neighbour path; on build 2 the backfill had filled them in, so it took the GPS path
  and moved an hour. Same source tree, two answers. Ask not just "is this field set?" but "who set
  it, and had they run the first time?" — `gps_source` distinguishes a measurement from a
  derivation, and only the measurement is this item's own evidence.
- **When one symptom has two causes, a fixed rule corrupts half the data.** An `OffsetTimeOriginal`
  that disagrees with GPS means either the clock was on another zone (tag holds the instant) or the
  tag is stale (wall reading holds it). Applying either unconditionally was right for 8 frames and
  wrong for 6. The tiebreak was already in the data: which reading lands next to the neighbouring
  frames from the same camera.
- **A geometry or measurement helper is as likely to be the bug as the code.** Three times in one
  cycle: `zip(xs, xs[1:], strict=True)` off by one, `ffmpeg -v error` silencing the
  `volumedetect` output being parsed, and `crop=w:h:x` silently centring `y` so two "different"
  bands sampled the same region. Each failed only because the assertion happened to reject equal
  sentinels. **Pair every measurement with a control that must differ from it.**
- **A file that is safe to read is not automatically safe to copy.** `story-book init` scaffolded
  a new trip by copying `overrides.example.toml`, which is a *worked example* naming photographs
  from the Europe trip — so the first `build` it told you to run died on
  `pin: no media in this library is named 'IMG_1880'`. Twelve tests passed; every one asked
  whether the file was *present*. When you copy a file into a new context, **load it in that new
  context** — `Overrides.load(scaffolded).is_empty` is one line. The starter is now derived from
  the example rather than kept beside it, because two copies of one file is one copy eventually
  wrong.
- **Look at real output.** A rendered contact sheet exposed the flat-score bug in seconds; no
  assertion had. Loading the report found that every image 404'd while the HTML validated
  perfectly — **when output references external files, resolve the references**, since the markup
  cannot be checked against itself. Read generated prose (`brief.md`, `prompt.md`) in the role of
  its consumer: field-presence tests pass on documents nobody could use.
- **A test of a failure mode must be shown to fail.** T42 corrupts the source tree on purpose in
  4 of its 16 tests. T43's resume check reported a pass three times without interrupting anything
  — `uv run` swallows SIGINT, a non-interactive shell's background jobs ignore it, and the output
  directory was never actually cleared. The exit code was the observation; everything else was
  inference.
- **If a stage's `select()` reads anything the runner doesn't know about, that is part of the
  cache.** `--force embeddings` was a silent no-op for exactly this reason. Override
  `Stage.clear_derived`.

### Hard-won gotchas

- Batch ExifTool into **one process for the whole run**. Per-file spawn is a ~20× slowdown.
- `DateTimeOriginal` has no timezone. Resolve via `OffsetTimeOriginal` → GPS lookup → nearest
  GPS-bearing item on the same device → config default, and store both naive local *and* UTC.
  Order by UTC, split days by local. This is the highest-risk logic in the project.
- pHash and CLIP solve *different* problems: pHash at a tight threshold for near-exact dupes and
  bursts, CLIP cosine at a looser one for semantically similar shots. Cluster within an event
  only.
- Pin GitHub Actions to exact versions. `releases/latest` tells you the version, not which refs
  resolve — `setup-uv` has no floating `v9`.
- `uv run` resolves the project from the cwd; don't `cd` away from the repo to test the CLI, pass
  absolute paths.

### Git

- Check `git log -1 --format='%ae'` before the first push of a new repo. This repo has a
  **repo-local** personal identity; global config is the work address.
- Remote: `github.com/victor-huang/travel-story-book` (public). CI runs macOS + Linux.
- Commit or push only when asked. End commit messages with the `Co-Authored-By` trailer.

## Reporting numbers

Quote a metric with its **sample size** and, where the decision is close, its **margin**. This
project has produced "100% precision" over 8 pairs (four visible false merges in the unlabelled
remainder) and "67% keeper agreement" over 6 groups where every decision turned on a 0.001-0.008
score gap. Both were arithmetically correct and told the reader nothing true.

And **look at the output**. Rendering contact sheets has three times found in seconds what the eval
harness could not find at all: flat quality scores, chained clusters, and a selection full of
parked vans.

## After a dev/test cycle

Add a retro entry to `retro/agentic_coding/YYYY-MM-DD-<slug>.md` for anything that cost real
time or shipped a wrong result — especially **tests that were wrong rather than code**, and
confident assertions that turned out false. If a lesson is cheap to enforce, encode it as a lint
rule, a test, or a line here, and say so in the entry. See that folder's README for the template.
