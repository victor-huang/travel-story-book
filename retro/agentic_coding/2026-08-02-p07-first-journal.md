# P07 — the first real journal: ids held, prose drifted (2026-08-02)

A full package went through ChatGPT and came back as a story. The architecture held. The two things
worth recording are one result I did not expect to be so clean, and one gap that had been invisible
for three reviews.

---

### The structured output stayed accurate and the free prose did not

**Category:** design — the most useful result of the whole project so far

**Observation.** The same model, in the same run, produced two documents about the same trip:

- `story.json` — thirteen chapters, twenty-three captions, every claim attached to an `asset_id`.
  The Plague Column sits in `2026-07-18-ch04`, which is where it belongs.
- `trip_summary.md` — readable editorial prose. It places the Pestsäule on **20 July**, as the
  "visual farewell to Vienna", two days from where the photograph was taken.

Nothing was wrong with the model. The difference is structural: an `asset_id` is checkable and a
sentence is not, so the id-bearing document could not wander and the prose could. Every one of the
56 references in `story.json` resolved; not one sentence in the summary was anchored to anything.

**Lesson.** **Grounding is a property of the format, not of the writer.** This is the strongest
argument yet for the decisions that felt fussy at the time — stable content-derived ids, "refer to
photos by `asset_id`, never by cell number", the manifest as the authoritative artifact. It also
sets the rule for what comes next: the book and the report render from `story.json`, and the prose
summary is a convenience for humans, never a source.

---

### Three reviews of the *input* contract, none of the *output* contract

**Category:** design-flaw
**Cost:** a non-conformant response that only a hand-read could detect

**Symptom.** Thirteen chapters, **zero** `source_event_ids` — a field the prompt explicitly asks
for. `video_scenes` renamed to `video_storyboard` and restructured into an object,
`uncertainties` to `global_uncertainties`, `layout_pages` and `requested_additional_context`
absent entirely. A renderer built to the contract would fail on the whole file.

**Root cause.** P05 asked for a JSON Schema and I shipped one — for `manifest.json`, the thing the
package *sends*. Nobody, including me, noticed that the thing the package *asks for* had no schema
at all. The prompt described a shape in prose and hoped. Three rounds of review hardened the input
side while the output side stayed a suggestion.

The asymmetry is easy to see in hindsight: I had been treating the package as an artifact to be
validated, when it is really one half of a protocol. A protocol has two message types.

**Fix.** `schema/story.schema.json` ships inside every package beside the manifest schema; the
prompt names the file and requires the key names verbatim; and `story-book check-story` validates a
response against it.

**Lesson.** **When you publish a request format, publish the response format with it.** The test
for whether a contract is real is whether something can check it — until `check-story` existed,
"the prompt asks for `source_event_ids`" was a sentence in a template, not a requirement.

---

### Two checks that had to be separate

**Category:** design

`check-story` reports shape and grounding independently, and the real response is exactly why:
**100% of its asset references resolved and it was still unreadable by a renderer.** Had the two
been collapsed into one pass/fail, that document would have looked simply "broken", when in fact
its factual anchoring — the hard part, the part a model can get subtly and dangerously wrong — was
perfect.

The grounding check also walks the *entire* document rather than the expected keys. That is
deliberate: the premise of the check is that the model may not have used the keys it was asked for,
so looking only where the contract says to look would have missed every reference in the renamed
`video_storyboard` block.

And the ordering in the output is by consequence, not by count: unknown references first in red
(a caption on a non-existent id reads as a fact), shape problems second in yellow (a renderer
fails loudly), uncited assets last in grey (information, not an error).

---

## Encoded as project rules

- `story.json` is the rendering source of truth. Generated prose is a human convenience and never
  an input to anything.
- A published request format ships with a published response format. Both schemas travel inside
  the package.
- Validate a model's answer on two axes separately: does it match the shape, and does every
  identifier in it exist? The second is the one that can lie.
- A grounding check walks the whole document, not the keys the contract names — the model may have
  renamed them, and a reference in an unexpected place still has to resolve.
