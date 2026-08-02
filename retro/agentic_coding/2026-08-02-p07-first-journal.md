# P07 — the first real journal, and a finding I got wrong (2026-08-02)

A full package went through ChatGPT and came back as a story. The architecture held: 56/56 asset
references resolved, 100% coverage, nothing dangling across 13 chapters and 23 captions.

The headline finding I reported from it was false, and the traveller corrected me. That entry is
first, because how I got there matters more than the two real gaps underneath it.

---

### I called a model's output wrong without looking at the photograph

**Category:** wrong-assumption — mine, not the model's
**Cost:** a false claim reported to the user, written into the plan doc, the tracker, `CLAUDE.md`,
a retro and a pushed commit message, and used as the headline finding of the whole review

**What I said.** That the generated `trip_summary.md` had moved the Pestsäule two days — placing it
on 20 July when the photograph was taken on the 18th — and that this showed structured output
staying grounded while free prose drifts.

**What is true.** The traveller was in the old town again on 20 July. `IMG_2072`, taken at 14:13
that day, is a column monument against a bright sky. The prose was describing a real photograph
from the day it named. And `story.json` makes the *same* claim in a caption — "One last look at the
Pestsäule before leaving Vienna" — so the two documents never disagreed at all. My example did not
exist.

**How I got there.** I searched the manifest for the asset carrying the plague column, found
`IMG_1868` on 18 July, saw the prose say 20 July, and concluded. Every step was a metadata lookup.
I never opened `IMG_2072`, which is one `Read` away and settles it immediately — as it did the
moment the traveller pushed back.

**Lesson.** The project's own first rule is **look at the output**, and I have written it into
`CLAUDE.md` three times, each time about an artifact I was producing: contact sheets, HTML pages,
generated briefs. It applies with more force to a claim about what is *in* a photograph. Metadata
tells you which file a caption points at; it cannot tell you whether the caption is true. **When
the claim is about image content, the image is the evidence — nothing else is.**

There is a second-order lesson about confidence. The finding was attractive: it was clean, it
flattered a design decision I had made, and it produced a memorable line. That is exactly the
profile of a claim that deserves one more check before it goes in a commit message. The retro two
weeks ago said "a metric over a handful of samples agrees with whatever you already believe" — this
is the same failure with n=1 and no metric at all.

**What survives.** Something smaller and real: the model hedged the *attraction* ("likely Time
Travel Vienna, but the package does not provide a confirmed landmark label") and asserted the
*column* flatly, though naming either is an inference from an image. The prompt asks for that flag.
`check-story` cannot catch it — both claims are structurally valid — so it stays a matter of prompt
wording.

---

### A fix that never reached the libraries that needed it

**Category:** design-flaw
**Cost:** every package the reviewers saw was built on drifted timestamps

**Symptom.** Re-checking the above, a fresh build of the same source produced a different day
structure: three stops on 19 July where the reviewed package had six, and no stop at 00:59.

**Root cause.** `media.exif_local` was added to stop the timezone stage reading a field it also
writes — the third instance of that bug shape, with its own retro. But `MetadataStage.version`
stayed at **1**. Metadata is cached per item, so on a library that already existed the stage never
re-ran, `exif_local` stayed NULL forever, the timezone stage fell back to `taken_local`, and the
drift the column was added to prevent carried straight on. The fix worked perfectly on new
libraries and did nothing at all for old ones — including the one every review ran against.

**Fix.** `MetadataStage.version = 2`, with a test that a cached v1 result no longer counts as done
and one that a real fixture comes back with `exif_local` populated.

**Lesson.** **Adding a column that a stage writes is a change to that stage, and requires bumping
its version.** The cache key is `(hash, stage, version)` precisely so this is expressible, and I
changed the schema and the writer without touching the number that connects them. The general form:
a fix that lives inside a cached stage reaches only the data that has not been computed yet, so ask
"what happens to a library that already exists?" as part of shipping it, not as part of debugging
it later.

### Three reviews of the *input* contract, none of the *output* contract

**Category:** design-flaw
**Cost:** a non-conformant response that only a hand-read could detect

**Symptom.** Thirteen chapters, **zero** `source_event_ids` — a field the prompt explicitly asks
for. `video_scenes` renamed to `video_storyboard` and restructured into an object,
`uncertainties` to `global_uncertainties`, `layout_pages` and `requested_additional_context`
absent entirely. A renderer built to the contract would fail on the whole file.

**Symptom, restated.** This one is real and stands unchanged.

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

- **A claim about what is in a photograph is settled by the photograph.** Metadata says which file
  a caption points at, never whether it is true.
- **Adding a column a stage writes means bumping that stage's version**, or the fix reaches new
  libraries only. Ask what happens to a library that already exists.
- `story.json` remains the rendering source of truth — not because prose was caught drifting here,
  but because it is the half a renderer can consume and a checker can verify.
- A published request format ships with a published response format. Both schemas travel inside
  the package.
- Validate a model's answer on two axes separately: does it match the shape, and does every
  identifier in it exist? The second is the one that can lie.
- A grounding check walks the whole document, not the keys the contract names — the model may have
  renamed them, and a reference in an unexpected place still has to resolve.
