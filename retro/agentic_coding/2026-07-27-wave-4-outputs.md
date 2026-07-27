# Wave 4 — outputs, and an acceptance test that lied three times (2026-07-27)

Report, package, non-destructiveness proof, and the twelve-criterion acceptance walk. The most
useful thing in this cycle was a test that kept passing when it should not have.

---

### An acceptance test that reported a pass three times without testing anything

**Category:** process, and the most dangerous kind
**Cost:** ~40 minutes, and it would have shipped a false claim about the project's second
non-negotiable guarantee

**Symptom.** Criterion 2 is "kill `build` mid-run and confirm clean resume". The script reported a
clean pass. It was wrong three times in a row, for three unrelated reasons:

1. **`uv run` does not forward `SIGINT` to its Python child.** The signal went nowhere; `wait`
   blocked for the full 3m44 build, which then exited 0.
2. **The output directory was not actually being cleared** between phases, so every "cold" build
   found a complete cache and finished in seconds. The `rm -rf` looked fine and did nothing
   useful.
3. **A background job in a non-interactive shell inherits `SIGINT` as ignored.** With job control
   off, `kill -INT` is a no-op. The same command had worked when I ran it interactively, which is
   what made this one so convincing.

**What should have caught it immediately:** the exit code. An interrupted build exits 130. Mine
printed `exit=0` every time, in a line I had written myself and then read past — three times. I
was reading the *derived* number (stage-result counts went up!) and ignoring the direct one.

**Fix.** Invoke the venv entry point directly, `set -m`, and assert the start state is empty
rather than assuming the `rm` worked. Real result: three interrupts at exit 130, results climbing
0 → 294 → 296 → 302, resume recomputing nothing already finished.

**Lesson.** **A test of a failure mode must be shown to fail.** I did this correctly for T42 —
four of its sixteen tests deliberately corrupt the source tree to prove the manifest notices —
and did not think to do it for T43, which is the harder and more important case. If a test says
"we survive X", make X happen and watch it go red *first*. And when a script prints a status code,
that code is the observation; everything else is inference.

---

### Valid HTML, every image broken

**Category:** design-flaw
**Cost:** caught in the first minute of looking, would have been invisible to any assertion I had
written

**Symptom.** The report rendered. The markup was correct. Every single image 404'd.

**Root cause.** Derived images live at `<out>/thumbs/`, the pages at `<out>/report/`, and the
asset paths in `trip.json` are relative to `<out>`. The templates used one relative prefix for
both the stylesheet and the media, so every `src` resolved one directory too deep.

**Fix.** A separate `media_rel` for paths that climb out of the report directory, and a test that
resolves every `src`/`href` on every page to a real file — plus a guard test asserting at least
one thumbnail *is* referenced, so the first test cannot pass by finding nothing.

**Lesson.** This is the fifth time "look at the output" has found something no assertion did, and
the first time the thing was completely invisible in the artifact under test: reading the HTML
tells you nothing about whether the path resolves. **When output references external files,
resolve the references — the markup cannot be checked against itself.**

---

### Two content defects only readable prose reveals

**Category:** design-flaw

Reading the generated `brief.md` as ChatGPT would, rather than checking it contained the right
fields:

- **Three stops in one day, all titled "Vienna, Austria"**, with the same string repeated on each
  of 33 photo lines. Every field was correct and the document was useless — a model has nothing
  to distinguish the cathedral from the concert hall. Fixed by numbering stops, dropping the
  per-photo place when it matches the stop's, and — the important part — *saying* that landmark
  recognition did not run, rather than letting identical headings imply there was nothing more to
  know.
- **The contact sheet captions carried the place, not the `asset_id`** — while the prompt
  instructs the model to reference photos by `asset_id`. It could see the picture and the cell
  number but had to consult a second document to name it. The id now goes on the cell.

**Lesson.** Field-presence tests pass on documents that no reader could use. For anything a human
or a model is meant to *read*, read it in the role of its consumer at least once.

---

### `--force` on a stage that filters against its own table

**Category:** design-flaw, found by the acceptance walk

**Symptom.** `--force embeddings` cleared 277 cache rows and then did nothing. The DB was left
with 277 embeddings and zero `stage_result` rows: permanently inconsistent, and the force silently
a no-op.

**Root cause.** `clear_stage` deletes `stage_result` rows, which is sufficient for any stage whose
only record of having run is that cache. `EmbeddingStage.select()` also filters against the
`embedding` table — deliberately, so that changing the CLIP model re-embeds — so it saw the work
as already done.

**Fix.** A `Stage.clear_derived(ctx)` hook that `--force` calls; any stage whose work list
consults something other than the runner's cache overrides it.

**Lesson.** A cache with two sources of truth needs both invalidated, and the second one is
invisible from the invalidation site. The generalisable rule, and the one now in the docstring:
**if `select()` reads anything the runner does not know about, that thing is part of the cache.**

---

### Two deviations from the plan, one justified and one not

**Category:** process

**Leaflet → inline SVG: justified, and documented at the decision site.** The criterion is offline
browsing; Leaflet needs a CDN for its JS and a tile server for every pan. Vendoring fixes the
first, not the second, and a map of grey rectangles is worse than none. SVG needs no JavaScript,
works from `file://` forever, and still shows the shape of the day and which fixes were
interpolated.

**Hand-rolled HTML instead of Jinja2: not justified.** I wrote 300 lines of f-string HTML because
I preferred it, when the plan named Jinja2 and the frozen layout already had a `templates/`
directory. The reason the plan says Jinja2 is that template iteration should be cheap for the
*user*, which my preference does not outweigh. Converted before committing.

**Lesson.** "The plan says X and I'd rather do Y" is only a decision when there is a reason
outside taste. Otherwise it is drift, and the frozen-layout file listing was sitting right there
saying so.

---

### A test that could only pass on my machine

**Category:** process
**Cost:** one red CI run, caught before it sat there unnoticed

**Symptom.** The new `--force` regression test spied on `EmbeddingStage.clear_derived` and asserted
the runner called it. Green locally, red on both CI runners.

**Root cause.** CI installs no CLIP extra, so `EmbeddingStage.available()` returns false and the
runner skips the stage before it ever reaches the force branch. The behaviour under test was fine;
the test had picked the one stage in the pipeline whose availability differs between my machine
and CI.

Worth noting the code is right, and deliberately so: forcing a stage whose dependency is missing
must *not* clear its derived rows, because that would destroy data nothing can regenerate. That is
now its own test.

**Lesson.** A test of *runner* behaviour should not depend on a *stage's* optional dependency.
Rewritten against a locally defined no-dependency stage. The general form: when testing a
framework, use a fixture double, not the most convenient real implementation — the real one drags
its environment in with it. This also cost a red CI run because I pushed and moved on; checking
the run is part of pushing.

---

## Encoded as project rules

- Any test asserting survival of a failure must be watched to fail first. T42 does this with four
  deliberate-corruption tests; T43's resume check now asserts its own start state.
- When output references files, a test resolves every reference — plus a guard that references
  exist at all.
- `Stage.clear_derived` for any stage whose `select()` consults something the runner cannot see.
- The report's departure from Leaflet is documented in `export/report.py` where someone changing
  it will read it, not only in the plan.
- Generated prose (`brief.md`, `prompt.md`) gets read in the consumer's role, not just asserted
  field by field.
