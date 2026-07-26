# Truth-set format

A truth set is a hand-written TOML file capturing what a human believes is *true* about a small
slice of a real trip: which photos belong to the same event, which photos are near-duplicates of
each other, and which photo in each duplicate group a human would keep. `src/story_book/eval.py`
loads it and scores the pipeline's actual output against it.

This is what makes `[dedup]`, `[quality]`, `[selection]`, and `events.jump_km` in
`config.toml` tunable instead of guesses -- see `dev_plan/p01_profile_findings.md`. It's also
what task **P03** (labelling ~200 real photos) will produce.

## Why TOML, why filenames

- **TOML**, because it's the format the rest of this project already uses for hand-edited files
  (`config.toml`, and `overrides.toml`), it supports comments (label reasoning, leave TODOs), and
  `tomllib` is in the standard library -- no new dependency for a file a human edits by hand.
- **Filenames as the primary key**, not content hashes. A human labelling from a Finder/Explorer
  window, a contact sheet, or a photo viewer sees `IMG_0042.jpg`, never a BLAKE2b digest. Forcing
  hashes into a hand-edited file would mean either computing them out-of-band before labelling
  (extra ceremony, exactly what this format tries to avoid) or making the file unreadable.

  The tradeoff: filenames aren't unique keys in general -- two different source folders can both
  contain an `IMG_0001.jpg`. The optional `[hashes]` table (below) records a content hash for any
  filename that turns out to be ambiguous, so the loader can disambiguate deterministically
  instead of guessing. In practice this only matters if your labelled set spans multiple import
  sources with colliding camera-assigned filenames; most single-trip truth sets never need it.

## Format

Every section is **independently optional** -- a truth set may label events but not duplicates,
or duplicates but not events, or one duplicate group with a `keep` pick and another without one.
The eval harness reports "not yet computed" for anything a file doesn't label, rather than
treating an absent section as "zero of everything."

```toml
[trip]
name = "Vienna & Salzburg 2026"      # optional, cosmetic only
notes = "days 2-3, labelled by vic"  # optional, cosmetic only

# --- Event boundaries ---------------------------------------------------------------------
# One [[event]] block per true event. List every photo that belongs to that event, in any
# order -- the harness re-derives chronological order itself from the pipeline's own
# timestamps, so you don't need to get the order right or maintain an event ID.
[[event]]
items = ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"]

[[event]]
items = ["IMG_0004.jpg", "IMG_0005.jpg"]

# --- Duplicate groups ----------------------------------------------------------------------
# Members that are near-duplicates of each other (see "what counts as a duplicate" below).
# `keep` is the photo a human would keep -- optional; a group can be labelled before you've
# decided on a pick.
[[duplicate_group]]
members = ["IMG_0001.jpg", "IMG_0002.jpg"]
keep = "IMG_0002.jpg"

[[duplicate_group]]
members = ["IMG_0006.jpg", "IMG_0007.jpg", "IMG_0008.jpg"]
# no `keep` yet -- fine, this group is still scored for clustering, just not keeper agreement.

# --- Distinct pairs (optional) ---------------------------------------------------------------
# Two photos you know are NOT duplicates of each other, even though they might look similar
# (two different fountains, two different plates of food). Without at least some of these, a
# photo the pipeline over-clusters but that never appears in any duplicate_group can't be
# caught -- there's nothing in the file saying it *shouldn't* have been merged with anything.
#
# Must be its own [[distinct_pair]] array-of-tables, not a bare `distinct_pairs = [...]` array:
# TOML attaches a bare key that follows an [[array-of-tables]] header to the *last table in
# that array*, not to the document root, so a bare key here would silently vanish into whatever
# [[duplicate_group]] came before it.
[[distinct_pair]]
pair = ["IMG_0003.jpg", "IMG_0009.jpg"]

# --- Hashes (optional) -----------------------------------------------------------------------
# Only needed to disambiguate a filename that exists more than once in the imported library.
[hashes]
"IMG_0001.jpg" = "b2:9f1c..."
```

See `tests/fixtures/truth_set_example.toml` for a complete worked example (it's a hand-written
toy, not real trip data).

## How to label consistently

**Event boundaries.** A new event starts when the trip's own story would say "then we went
somewhere else" -- a change of place, or a long pause (lunch, a transit leg, back at the
hotel), not just a gap in shutter presses. Two photos ninety seconds apart at the same
viewpoint are the same event; two photos five minutes apart because you walked from one room of
a museum to the next are usually still the same event (it's one visit); crossing from the museum
to a cafe afterward is a new one. When genuinely unsure, prefer *not* splitting -- the acceptance
criterion treats a merged pair as a single miss, so an event boundary you're on the fence about
costs less to leave out than to invent.

**Duplicate vs distinct.** Two photos are a duplicate pair if you'd only ever want to keep one of
them in a final album -- the same subject, same moment or same held pose, taken moments apart
(burst shots, "just in case" retakes, the same landmark from virtually the same spot). They are
*distinct* if either would earn its own place in the album: different angles that show something
the other doesn't, a establishing wide shot vs. a detail close-up, or the subject having visibly
moved/changed (different expression, different pose, people walking through frame). When in
doubt, treat it as distinct -- the plan doc is explicit that a false merge of visually distinct
subjects is worse than a missed duplicate.

**Keeper pick.** Whichever photo you would personally keep if forced to delete the rest of the
group: normally the sharpest, best-exposed, best-composed one. Ties (two equally good shots) are
real and fine to record with a comment (`# tied with IMG_0007, picked for the exact center
composition`) even though only one filename can go in `keep`.

## What the eval harness reports

Run `evaluate_truth_set_file(conn, path)` (see `src/story_book/eval.py`) against a trip
database, then `render_report(report)` to print it. Three metrics, matching the plan doc's
"Success criteria for Phase 1":

1. **Event boundaries** -- precision/recall/F1 over adjacent-pair boundaries in chronological
   order. A truly-separate pair of events the pipeline merges into one costs exactly one missed
   boundary (not one miss per pair of photos on either side of it), matching "a merged pair
   counts as one miss." Target: **>=80% precision and recall**.
2. **Duplicate groups** -- pairwise precision/recall over "are these two photos in the same
   cluster" across every pair of labelled items. Chosen over Adjusted Rand Index because the
   plan calls out an asymmetry ARI would hide: pairwise *precision* is directly "of the pairs the
   pipeline merged, how many really are duplicates" (the false-merge rate the plan says matters
   most), while pairwise *recall* is "of the true duplicate pairs, how many did it find." A
   single ARI number can't tell you which failure mode you're looking at; two numbers can.
3. **Keeper agreement** -- fraction of duplicate groups (that have a `keep` label and resolve to
   a real pipeline cluster) where the pipeline's `cluster.keeper_hash` matches the human pick.
   Target: **>=70%**.

Every metric independently reports `computed=False` with a note instead of a number when it
can't be scored: the relevant truth-set section is empty, fewer than two labelled items resolved
to real media, or the pipeline hasn't produced `event`/`cluster` rows yet (T23/T24/T30 don't
exist as of T18). Filenames the truth set mentions that aren't in the DB are reported separately
as `unmatched_filenames`, never as a crash.

## Recommendation for labelling ~200 photos (P03)

- **Pick 2 full days plus a scattered sample from 1-2 more**, rather than 200 photos spread
  evenly across the whole trip. Two full days give the event-boundary metric enough true
  boundaries to be statistically meaningful (the plan doc's own P01 data found only ~15-25
  boundaries per few hundred photos), and a full day also naturally yields the bursts and near-
  duplicates that make the dedup metric meaningful. The scattered extra day(s) add diversity
  (different pace, different device mix) without doubling the labelling time.
- **Budget roughly half a day (3-4 hours)**, matching the plan doc's own estimate ("the single
  most useful half-day in the project"): about 45-60 minutes per day to sort photos into event
  groups, and the rest identifying duplicate/burst clusters and picking a keeper in each --
  expect on the order of 15-25 duplicate groups per labelled day at typical burst-shooting
  density (P01 found roughly half of all consecutive shots within a minute of each other).
- **Label events first, then duplicates within each event** -- clustering is only meaningful
  within an event boundary anyway (Module 7 explicitly never clusters across events), so doing
  events first gives natural, smaller batches to sort for duplicates.
- Leave `keep` blank on any group where you're genuinely torn; a partially-labelled group still
  contributes to the duplicate-clustering score, just not keeper agreement.
