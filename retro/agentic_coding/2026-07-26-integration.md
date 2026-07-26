# Integration pass after the Wave 1 fanout (2026-07-26)

Wired nine independently-built stages into a running pipeline. Every stage arrived with a passing
suite; the integration found **six bugs**, and not one of them was findable by a unit test.

The pattern is worth stating up front, because it recurs in all six: each stage was correct in
isolation. The bugs lived in **seams, second runs, and real data** — three things a per-stage
test suite structurally cannot see.

- **Seams:** a stage writes a field the next stage never reads, or vice versa.
- **Second runs:** caching means the second `build` takes different code paths than the first.
  Every test ran the first path.
- **Real data:** synthetic fixtures are chosen to have the property under test. Real photos have
  properties nobody chose.

---

### The EXIF offset was computed and thrown away

**Cost:** would have silently disabled the primary timezone path forever
**Category:** seam

**Symptom.** None. Both stages green. `tz_source` was `UNKNOWN` on every row.

**Root cause.** `metadata` parsed `OffsetTimeOriginal` into an `ExifTimestamp` and never persisted
it. `timezones` reads `tz_offset_minutes`/`tz_source` to decide whether a validated EXIF offset is
available, so **level 1 of the resolution order could never fire** and every photo fell through to
GPS. On this library that produced *plausible* output — GPS is a good fallback — while the
documented primary path was dead code.

**Fix.** Two lines in `_apply`. Found by the T12 agent reading T11's code, not by any test.

**Lesson.** **A handoff needs a test that spans both sides.** `tests/backend/test_pipeline_integration.py`
now exists for exactly this: it asserts `tz_source is EXIF_OFFSET` after running metadata *and*
timezones, which no single-stage test could express. Also: the agents' instruction to report
cross-boundary problems rather than patch them is what turned this into a diagnosis instead of a
merge conflict with T11's concurrent writes.

---

### Re-scanning wiped every computed field

**Cost:** near-miss, would have been silent data loss on every build
**Category:** second run

**Symptom.** Found by a new integration test asserting metadata survives a re-scan. It didn't.

**Root cause.** `scan` constructs a fresh `Media` carrying only discovery fields and called
`upsert_media`, whose `ON CONFLICT DO UPDATE` writes *every* column — so `taken_local`, GPS, and
all `tz_*` fields were overwritten with NULLs.

This was about to get much worse. I was mid-way through adding `always_run` to `scan` (so it would
notice newly added files). With both changes, **every build would blank the database**, while the
stages that repopulate those fields saw cached `ok` results and skipped. Two individually sensible
changes composing into total data loss.

**Fix.** `upsert_media_discovery`, which touches only the columns the scanner actually knows about.

**Lesson.** **An upsert that writes every column is a footgun for any partial writer.** Ownership
of *columns*, not just tables, has to be explicit. And when adding a flag that makes a stage re-run,
check what that stage overwrites — "runs more often" and "overwrites too much" are individually
harmless and jointly fatal.

---

### Aggregate stages went stale on incremental adds

**Cost:** a newly added photo silently vanishes from the story
**Category:** second run

**Symptom.** Added one photo to a built trip. `scan` found it, `metadata` processed it, and
`timezones` reported `1 cached / 0 done`. The photo's `taken_utc` stayed NULL.

**Root cause.** `timezones` is a `WholeTripStage` cached under `TRIP_SENTINEL`. Its cached result
goes stale the moment the media set changes, but nothing expresses that dependency. A NULL
`taken_utc` isn't an error — the photo just drops out of ordering, day grouping, and the timeline.
Invisibly.

**Fix.** `always_run` on `timezones`, and the rule documented on the flag itself: **discovery
stages and aggregates over the whole media set both need it**; per-item stages never do, since
their cache is keyed per item.

**Lesson.** Cache keys must cover everything the result depends on. A whole-trip stage keyed on a
constant sentinel is really keyed on "nothing", which is only correct if its input can't change.
The next stages to land — days, events, selection, timeline — are all in this category.

---

### HEIC was never registered in production

**Cost:** every HEIC failed, with a green suite
**Category:** real data + a test that verified the wrong thing

**Symptom.** First real end-to-end run: 45 failures, all tracing to
`UnidentifiedImageError: cannot identify image file ... .heic`.

**Root cause.** Pillow needs `pillow_heif.register_heif_opener()`. The *fixture test* called it
itself before decoding. So the suite proved the **library** works and never proved the
**application** had registered it. HEIC is the dominant iPhone format and the plan calls it "the
first real portability trap" — it was, just not where expected.

`cv2.imread` compounded it: OpenCV cannot read HEIC at all, and registering a Pillow opener does
nothing for it. So quality scoring failed HEIC even after the fix, until image loading moved to
Pillow entirely.

**Fix.** Registered at package import in `story_book/__init__.py`, covering the CLI, every stage,
and any test importing either. One loading path (`_load_bgr`, via Pillow) for everything the
scanner accepts.

**Lesson.** **A test that performs setup the application doesn't perform is testing a fiction.**
If a fixture needs a line to work, ask who runs that line in production. This is the most
transferable lesson of the pass: the test wasn't wrong about the library, it was wrong about the
system.

---

### One bad file failed its entire batch

**Cost:** 22 good photos lost per bad file
**Category:** real data

**Symptom.** A single unreadable HEIC failed all 22 co-batched items in embeddings and
content-class. `QualityStage`, being per-item, correctly failed only the one.

**Root cause.** `BatchStage.process_batch` receives a group; an exception anywhere in it fails
everything. The T14 agent had already flagged the *skip* version of this asymmetry. The *failure*
version is worse, because bad files are ordinary.

**Fix.** Both stages try the batch, then fall back to per-item on any exception, so only the
genuinely broken file fails. Batching keeps its speed on the happy path.

**Lesson.** Batching for throughput trades away fault isolation, and that trade must be paid back
explicitly. "Fast path, then isolate on failure" costs nothing when nothing is wrong.

---

### The content classifier called 209 of 277 travel photos "screenshot"

**Cost:** would have rejected three quarters of the trip from highlights
**Category:** real data

**Symptom.** Real-data class distribution: `screenshot` 209, `landscape` 48, everything else in
single digits. `screenshot` is in `reject_content_classes`.

**Root cause.** Zero-shot labels were passed as bare tokens — `"screenshot"`, `"landscape"`,
`"group_photo"` with an underscore. CLIP's text tower is trained on captions and produces poor,
biased embeddings for bare words.

The fixture tests passed because `screenshot.jpg` really is a screenshot and `receipt.jpg` really
is a receipt. They measured **true-positive precision on the positive class** and never the
**false-positive rate on ordinary photos** — of which the fixture set had none that looked like
real travel photography.

**Fix.** Natural-language prompt ensembles per class, probabilities summed. Rejected share fell
from **77% to 6%**, with a plausible distribution (150 landscape, 80 other, 22 group photos).

A regression the fix itself introduced, caught by an existing test: an unusable provider response
became all-zero totals, and `max()` over all-zeros returns the first class — `screenshot` again. So
"no answer" would have been recorded as "reject this photo". Empty stays empty now.

**Lesson.** **A classifier tested only on unambiguous positives is untested.** The expensive error
here is the false positive, because the class is destructive. Test the class you'd hate to be wrong
about, on inputs that should *not* trigger it.

---

### Sharpness was collapsed to zero for every real photo

**Cost:** the highest-weighted quality signal contributed nothing
**Category:** real data

**Symptom.** Every score on the real library was ≈0.41. Noticed by *looking at a contact sheet*,
not from a test. Sharpness: p50 **0.0012**, stdev **0.009**.

**Root cause.** `_sharpness_component` divided Laplacian variance by pixel count as a "resolution
normalization". But Laplacian variance is a per-pixel statistic that does not grow with image size,
so dividing a typical variance of ~2000 by 12 million pixels annihilated it. Sharpness carries the
largest weight (0.40), so `overall` was effectively exposure+contrast compressed into 0.13–0.42.

**The unit test could not catch this.** It compared a sharp and a blurred image *of the same size*
and asserted ordering. The bug preserves ordering perfectly while destroying range.

**Fix.** Measure at a fixed 512px short edge (resolution genuinely matters — downscaling removes
the high frequencies being measured) and normalize against a reference constant calibrated on real
photos. Sharpness stdev **0.009 → 0.225**; overall **0.037 → 0.125**.

**Lesson.** **Ordering tests don't test calibration.** A monotone signal can be correct in order and
useless in range, and a weighted score built from it is quietly dominated by its other terms. When
a component feeds a weighted sum, assert its *spread* on realistic input, not just its direction.
Also: eyeballing real output found this in seconds. Rendering a contact sheet and looking at it was
worth more than any assertion I wrote that day.

---

## Encoded as project rules

- `tests/backend/test_pipeline_integration.py` — cross-stage seams, including the offset handoff
  and the full add-a-file-to-a-built-trip path, driven through the real `Runner`.
- `upsert_media_discovery` exists so partial writers can't blank columns they don't own.
- `always_run` documents both categories that need it, and warns that per-item stages don't.
- HEIC registered at package import; a single Pillow-based loading path.
- Batch stages fall back to per-item on failure.
- Prompt ensembles required to be natural language, with a test asserting no prompt is a bare
  token and none is just the class name.
- Sharpness has resolution-invariance and non-collapse tests that would have caught the original.
- Fixture counts in tests are now derived from the directory, so adding a fixture doesn't break
  unrelated assertions.
