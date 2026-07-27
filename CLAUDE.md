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
uv sync                                          # deps; --extra vision/video/exif for heavy stages
uv run pytest                                    # 753 tests, expect 0 failures 0 skips locally
uv run pytest tests/unit                         # fast, mocked, no DB
uv run story-book build <src> --out <dir>        # the pipeline
uv run story-book report --out <dir>             # re-render HTML only
uv run story-book profile <src>                  # folder stats + suggested config
uv run story-book eval <truth.toml> --out <dir>   # score against a labelled truth set
uv run python tests/fixtures/generate.py         # regenerate fixtures (deterministic)
```

Before committing: `uv run autoflake -i --remove-all-unused-imports $(find src tests -name '*.py')`
then `uv run ruff check . && uv run ruff format . && uv run pytest`.

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
scan → metadata → timezones → gps_backfill → geocode → days → events
     → (embeddings ∥ quality ∥ video) → dedup → selection → landmarks → timeline
     → (html report ∥ chatgpt package)
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
- **Look at real output.** A rendered contact sheet exposed the flat-score bug in seconds; no
  assertion had.

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
