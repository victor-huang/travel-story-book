# P05 — the package reviewed, and what a reviewer found that tests could not (2026-07-28)

The generated package went through ChatGPT. Verdict: usable for journal writing, not ready to
drive a renderer. Ten requests, eight implemented, two declined. The interesting part is *why the
tests could not have caught most of this*.

---

### Most of what was missing was already computed and simply not exported

**Category:** design-flaw
**Cost:** none yet, but it is the second time this exact shape has appeared

**Symptom.** Of the reviewer's ten points, five were fields the pipeline already had:

| asked for | where it already lived |
| --- | --- |
| UTC timestamps and offsets | `media.taken_utc`, `media.tz_offset_minutes` |
| width, height, orientation | `media.width` / `media.height` |
| video keyframes with offsets | `video_meta.keyframe_paths`, times deterministic from duration |
| captured vs included counts | both derivable, collapsed into one field |
| selection reasons | `selection.reason` and `rank` |

P02 found the same thing: *"All of this already exists in `video_meta`; only the export omitted
it."* Two independent reviews, the same category of defect — **the export layer narrows what the
pipeline knows, and nothing in the codebase notices.** Every stage test passes, `trip.json`
validates, the manifest validates, and the artifact is still poorer than the database behind it.

**Lesson.** A schema test asserts that what you emit is *well-formed*, never that it is
*complete*. There is no test that can ask "should this field have been here?" — that question
needs a consumer. The cheap approximation, and the one I will use next time an export is written:
**enumerate the source table's columns and justify each omission out loud**, rather than
assembling the record from what came to mind.

---

### Adding a timezone offset broke duration arithmetic, and the fix was a rule I already knew

**Category:** correctness, caught by an existing test

**Symptom.** Appending `+02:00` to `taken_local` made five video tests fail with
`can't subtract offset-naive and offset-aware datetimes`.

**Root cause.** Event durations were computed by subtracting two `taken_local` strings. Once some
items carry an offset and others do not — which is any real library, since offset resolution can
fail — the subtraction raises. And where it does not raise, subtracting two wall times across a
zone change is simply *wrong*: an event spanning a DST boundary reports an hour it did not last.

**Fix.** Durations come from `taken_utc`. Which is the project's own standing rule, written in
`CLAUDE.md` months ago: **order by UTC, split days by local.** A duration is an interval, so it is
UTC's business. I had applied the rule to ordering and to day boundaries and never to arithmetic.

**Lesson.** A rule stated for two cases does not announce the third. This is the same failure as
the rowid one in T31 — "identity is unstable" applied to overrides but not to the artifact — and
the read-and-write-the-same-field one before that. The recurring shape is **a correct principle
applied at the sites where I first learned it, and nowhere else.** The counter-move is to ask, at
the moment of learning, *where else does this class live* — and the answer is usually findable with
a grep.

---

### Declining two requests, and why that is the right answer

**Category:** design

**A place `confidence` number.** The reviewer asked for `"confidence": 0.98` beside each place. The
offline geocoder returns a nearest populated place from a bundled dataset and produces no
confidence at all. Any number here would be invented, and a fabricated measurement that *looks*
like a measurement is the failure mode this project has been bitten by repeatedly — the flat
sharpness scores, the "100% precision" over eight pairs, the unreachable face saturation. The
manifest now reports `source: "offline"` and `precision: "city"`, which is what is actually known.

**Automatic video `highlight_ranges` with scores and reasons.** Unlike the five items above, this is
genuinely new analysis — per-window motion and stability scoring — not unexported data. Shipping
plausible ranges would let a renderer cut on a number nobody computed. The keyframes and their
offsets give a model enough to choose a range itself, and the automatic version is logged for
Phase 2.

**Lesson.** "The reviewer asked for it" is not sufficient reason to emit a field. The test is
whether the value would be *true*. Declining is cheap when the reason is stated at the decision
site and in the plan; silently emitting a plausible number is what costs you later.

---

### The shipped schema caught a gap in the thing it describes, immediately

**Category:** process — a small win

Adding `manifest.schema.json` to the package and validating against it failed on the first run:
`'timezone' is a required property` on the trip block. I had added trip-level timezone to
`trip.json` and not carried it into the manifest, which copies specific fields.

A schema written *as a contract for a consumer* found a defect a schema written to match the code
never would have. Worth remembering when the temptation is to generate the schema from the
implementation: the value is precisely in it being an independent statement of what should be
there.

---

## Encoded as project rules

- When writing an export, enumerate the source table's columns and justify each omission. Two
  independent reviews have now found the export layer narrowing what the pipeline knows.
- Durations and intervals come from `taken_utc`, never from local wall time. `taken_local` carries
  its offset so the string is self-describing, which makes the naive-vs-aware mistake loud instead
  of silent.
- No confidence, score, or measurement is emitted unless the pipeline computed it. `precision:
  "city"` beats an invented `0.98`.
- The manifest schema ships inside every package and is validated in tests, as an independent
  statement of the contract rather than a description of the code.
