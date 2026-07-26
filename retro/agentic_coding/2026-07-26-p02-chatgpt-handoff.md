# P02 — the ChatGPT handoff test (2026-07-26)

The make-or-break check: assemble one real day's package, paste it into ChatGPT, judge the output.
**The format passed.** The journal draft was chronologically accurate and usable, the captions
matched what is visible, and no screenshot or receipt reached the 29 selected photos.

Then the reviewer listed ten problems. Triaging those — rather than implementing all ten — is the
substance of this entry.

---

### The most valuable output of a validation test is the list of things it invalidates

**Category:** process

The point of P02 was never "does it work". It was "is the target correct". The answer came back
*yes, and here are seven things the target is missing* — with three of the seven being data the
pipeline **already computes and simply failed to export**:

- Video duration, poster frames, keyframes, motion score — all sitting in `video_meta`, none of it
  in the brief. So the requested storyboard could only be invented from stills, which directly
  contradicted the prompt.
- A stable asset identity — the BLAKE2b content hash, used as the primary key throughout the
  database, absent from the package. The only IDs exposed were contact-sheet cell positions, which
  change whenever selection changes.
- Component quality scores — computed, stored, and reduced to a single opaque `0.88` on export.

**Lesson.** An export is a design surface, not a serialization detail. Three of seven gaps were
"the pipeline knows this and didn't say it", which is a failure of the *interface*, not the
engine. Worth asking of any output: what does the producer know that the consumer needed?

---

### Positional identifiers are not identity

**Category:** design-flaw

**Symptom.** The reviewer could say "use `01-10`" but nothing downstream could resolve that to a
file. Change the selection rules and `01-10` becomes a different photo.

**Root cause.** The package leaked an internal layout artifact as its public identifier. Cell IDs
are excellent for the *conversation* — a human and a model can both point at a grid cell — and
useless as a key.

**Fix.** `manifest.json` becomes the authoritative artifact, carrying a stable `asset_id` mapped to
source filename, content hash, event, capture time, export path *and* cell ID. `brief.md` is
generated from the manifest rather than maintained beside it.

**Lesson.** Two different jobs — "what can a human point at" and "what does a machine resolve" —
need two different identifiers, and only one of them may be positional.

---

### "Processed and found nothing" must be distinguishable from "not processed"

**Category:** design-flaw

**Symptom.** With the transcript hallucinations correctly discarded, videos appeared in the brief
with no transcript at all. From the outside that is indistinguishable from transcription never
having run.

**Root cause.** Absence encodes two different facts. The reviewer named it exactly: *"Even when no
speech exists, explicitly saying `no_speech` is useful. Otherwise the model cannot distinguish
missing processing from silent footage."*

**Fix.** `transcript_status: no_speech | absent | present` in the manifest.

**Lesson.** This is the same principle already applied inside the pipeline — `face_count` is NULL
rather than 0 when no detector ran, because "could not look" is not "nobody there" — and it had not
been carried through to the export. A rule worth applying anywhere a consumer reads a missing
field: **make the negative explicit, or it will be read as unknown.**

---

### The one input that cannot be extracted

**Category:** design gap

**Symptom.** The journal was accurate and impersonal. The reviewer diagnosed it unprompted:
*"This draft is chronologically grounded, but it lacks personal reactions because none were
included in the package."*

**Root cause.** Every other input to this system is derived from the media. Who was present, whose
voice the journal is in, what was planned versus stumbled into, what mattered — none of that is in
the pixels, and no better vision model recovers it.

**Fix.** A new task (T33): a small, entirely optional trip-context file. Travellers with optional
aliases, journal voice, known plans, free-text notes. The pipeline must produce a complete package
without it, and when it is absent the prompt must instruct the model to stay factual rather than
manufacture feelings — an invented emotion is the same class of failure as an invented quote.

**Lesson.** Probably the highest output-quality gain per line of code in the project, and it is
not a model or an algorithm. When output feels generic, check whether the missing thing is
*capability* or *input*.

---

### Where I pushed back

Accepting all ten items would have been worse than accepting seven.

**The missing `full/` directory was a test artifact, not a defect.** The reviewer's first and
longest complaint was that originals were absent, so it could not judge focus, blink, or crop
headroom. Correct — but the package does contain them; they simply were not uploaded. No design
change follows. The real lesson is smaller: the package should *declare* whether it is
preview-only, so the recipient is told rather than left to infer.

**Aesthetic and composition scores: rejected.** The suggested schema included
`"composition": 0.83, "aesthetic": 0.89`. The plan explicitly defers learned aesthetic models to
Phase 2. Shipping those fields would mean either inventing numbers or implementing deferred scope
because an example listed it. Ship the components that exist; say nothing about the ones that
don't.

**The four-way content taxonomy: deferred, principle accepted.** `exclude` /`archive-only` /
`scrapbook-candidate` / `story-evidence` is a genuinely better model than keep/reject — a ticket or
menu is scrapbook material, not trash. But Phase 1's job is narrower: keep screenshots out of
highlights. The taxonomy expands both the classifier's label set and selection's logic.

**Semantic chapters: reshaped rather than adopted.** The observation was sharp and correct — what
Module 6 produces is a time-and-location cluster, and one real cluster held a church interior, the
State Opera, and an evening walk: chronologically right, three stories. But building a
semantic-segmentation stage to fix it inverts the architecture. Instead: **clusters stay mechanical
and stay the pipeline's output; chapters are proposed by the model in its structured output and
approved by the human via `overrides.toml`.** Judgement lives where the judgement is, and a wrong
guess stays cheap.

**Lesson.** Detailed, well-reasoned review invites wholesale adoption, and its suggestions arrive
with more apparent authority than they have earned — the schema examples in particular look
authoritative because they are concrete. Each item still has to be weighed against the plan's
existing scope decisions, and "a reviewer suggested it" is not a reason to undefer Phase 2 work.

---

### An unexpected result worth measuring later

Given only coordinates and contact-sheet thumbnails, the model correctly named the Hofburg,
St Stephen's Cathedral, the Vienna State Opera and the Musikverein — while simultaneously arguing
that asking a model to resolve coordinates is fragile and should be replaced by backend
geocoding.

Both can be true: it may have recognized the buildings from the images and attached plausible
coordinates afterward, which is exactly the failure mode it warned about. But it does suggest the
marginal value of a dedicated landmark-recognition pass may be smaller than assumed for *famous*
landmarks in a well-fed package. That is now a note on Module 11: measure a package with landmark
labels against one without, judged on the journal, before committing to per-trip API spend.

**Lesson.** A validation test can quietly answer a question you didn't ask. Landmark recognition
was scoped on the assumption it was necessary; there is now evidence worth checking before paying
for it.

---

## Encoded as project rules

- Plan doc Module 14 carries the P02 result and all seven additions, with the rejected and
  deferred items recorded *as* rejected and deferred, and why.
- New "Trip context" section in the plan; new task T33 in the tracker.
- Module 6 amended (recent-window comparison) and Module 11 annotated with the landmark data point.
- Clusters-vs-chapters resolution documented: mechanical clusters, model-proposed chapters,
  human approval through overrides.
- Tracker cross-task table records each item against the task that must act on it, including the
  two deliberately deferred.
