# T43 — Phase 1 acceptance on the real trip (2026-07-27)

Every numbered success criterion from the plan doc, walked against the real 286-item Vienna
export. Verdicts are what the evidence supports, not what would be nice to claim.

**8 pass · 1 superseded · 1 not met (within noise) · 2 need a human**

| # | Criterion | Verdict |
| --- | --- | --- |
| 1 | `build` completes on the full trip | **PASS** |
| 2 | Kill mid-run, resume without recomputing finished work | **PASS** |
| 3 | Source directory provably unmodified | **PASS** |
| 4 | Days and events match hand labels ≥80% | **SUPERSEDED** — the criterion measures a category error |
| 5 | Duplicate groups correct; keeper agreement ≥70% | **NOT MET** — 67% over 6 groups, decided by noise |
| 6 | Screenshots/receipts/documents out of highlights | **PASS** |
| 7 | Every item on the map, interpolated distinguished | **PASS** |
| 8 | Report browsable offline; re-render under 10 s | **PASS** |
| 9 | Overrides apply without recomputing a cached stage | **PASS** |
| 10 | `prompt.md` + sheets yield a usable journal | **NEEDS A HUMAN** — met at P02; the additions are untested end-to-end |
| 11 | Photos near home excluded from every export | **NOT EXERCISED** by this trip — no home configured, no home-area media |
| 12 | `--no-cloud` produces a complete result | **PASS** |

---

## 1. `build` completes on the full trip — PASS

286 items (277 photos, 9 videos, 1.9 GB) → **3m44s** for a cold build, 1,699 stage results,
**0 failed**. No wall-clock requirement applies; this is well inside "overnight is fine".

## 2. Kill mid-run and resume — PASS

Three real `SIGINT`s to a cold build, each confirmed by **exit code 130**, plus two more targeted
at late stages via `--force`:

| interrupt | exit | stage results before → after |
| --- | --- | --- |
| 8 s | 130 | 0 → 294 |
| 25 s | 130 | 294 → 296 |
| 60 s | 130 | 296 → 302 |
| inside `thumbnails` | 130 | 277 → 9 kept, resume did the remaining 268 |

Resume to completion: 151 s, no finished item recomputed (`metadata 286 cached / 0 done`).

**This test lied to me three times before it worked**, and each lie looked like a pass:
`uv run` does not forward `SIGINT` to its Python child; the output directory was not actually
being cleared between phases, so every "cold" build found a complete cache; and a background job
in a non-interactive shell inherits `SIGINT` as *ignored*, so `kill -INT` was a no-op. The tell in
all three cases was the exit code — an interrupted build exits 130, and mine kept exiting 0.

## 3. Source directory provably unmodified — PASS

`tests/backend/test_non_destructive.py`, 16 tests, in CI. A full content-hash manifest of the
source tree before and after a build, plus sizes, mtimes and the directory listing, because a
rewrite with identical bytes is still a write. Also a `Path.open` guard that fails on any
non-read-mode open under the source — catching intent, not just outcome — and an assertion that
an `--originals` package leaves every original in place.

Four of the sixteen tests break the tree deliberately to prove the proof can fail.

## 4. Days and events match hand labels ≥80% — SUPERSEDED

Measured: **precision 25%, recall 29%.** The criterion is not met and should not be chased.

P03 established why, by measurement: human chapter boundaries fall at 2- and 8-minute gaps and
10–230 m, against ordinary within-event movement of 2.8 km — *anti*-correlated with both signals.
A grid search over gap, jump and duration tops out at F1 57%; adding CLIP content distance makes
it worse (33%). The information is not in the metadata.

Events were therefore redefined as **internal scoping only** — they bound deduplication, allocate
highlights, and sample landmark recognition, and are never shown to a reader. Scoring them against
human *chapter* labels compares two different things. The plan doc has been amended.

## 5. Duplicate keeper agreement ≥70% — NOT MET

**67% (4 of 6 groups).** One group short of target, on a sample of six, where every decision
turned on a score gap of 0.001–0.008. That gap is noise: 4-of-6 is a coin flip that landed well,
not a capability, and 5-of-6 would have been the same.

Technical quality cannot separate two frames of one burst. What separates them is expression and
whether someone blinked, which needs either face analysis beyond detection (Phase 2) or a human —
and the human now has `overrides.toml`'s `keeper` list. Duplicate *grouping* is sound: precision
100%, recall 89%, with the false merges the user found in review actually gone.

**Follow-up:** either label more groups, or drop the criterion in favour of one about the override
path. Logged in the tracker.

## 6. Screenshots, receipts, documents out of highlights — PASS

**0 of 47** day highlights carry a rejected content class.

## 7. Every item on the map, interpolated distinguished — PASS

**286 of 286 located** (6 by interpolation, after GPS backfill). Interpolated fixes render as
hollow dashed markers against solid ones, with a legend and a count per day. This matters because
the interpolated points are the ones the map might be lying about.

## 8. Offline, and re-render under 10 s — PASS

**0.3 s** to re-render 5 pages. The only external references in the entire output are four
optional "open this area in OpenStreetMap" links; nothing is *fetched* from the network, and
there is no `<script>` tag anywhere.

This is why the map is inline SVG rather than Leaflet — see the note in `export/report.py`.
Leaflet needs a CDN for its own JS and a tile server for every pan, and a map of grey rectangles
is worse than no map.

## 9. Overrides apply without recomputing a cached stage — PASS

17 pins and 4 rejects applied; **0 per-item stages recomputed**. Only the always-run aggregates
(events, dedup, selection, timeline) re-derive, which is by design and takes about three seconds.

## 10. Journal quality from `prompt.md` + sheets — NEEDS A HUMAN

Met at P02 by hand, which is what validated the format. The seven additions P02 asked for are all
implemented and covered by 34 tests, but "produces a journal needing only light editing" is a
human judgement and the *new* package has not been through a real ChatGPT session. The one thing
P02 could not produce — a video storyboard referencing actual footage — is now possible: all nine
clips are in the package with durations and explicit `no_speech` status.

**Open for the user:** run one day's package through ChatGPT and confirm.

## 11. Home exclusion — NOT EXERCISED BY THIS TRIP

`home` is unset in this config and the export contains no home-area media, so the filter had
nothing to act on. The report and `trip.json` both say so explicitly rather than reporting zero
exclusions as a clean result.

The behaviour is enforced and asserted in `tests/backend/test_home_filter.py`, and a dedicated
test confirms that even a **pinned** photo near home stays out — a human override beats the
quality floor and the content filter, but never the privacy guarantee.

**Open for the user:** set `config.home` to exercise this on real data.

## 12. `--no-cloud` produces a complete result — PASS

Every build in this acceptance ran `--no-cloud`: 286 items, 47 highlights, a full report and a
full package. Only the landmark stage skips, and it announces itself.

The visible cost is in the package: with no landmark names, every stop on a single-city day is
labelled "Vienna, Austria". The brief now says so — *"Landmark recognition did not run, so stops
are named by city only; do not assume two stops in the same city are the same place"* — rather
than letting three identical headings imply the pipeline had nothing more to offer.
