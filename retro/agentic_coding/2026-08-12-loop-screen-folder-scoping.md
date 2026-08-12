### A finished job silently reused when a different folder was sent

**Cycle:** I26, found live during the human's first hands-on test session
**Cost:** ~20 min diagnosis + fix; would have shipped as a confusing UX bug
**Category:** wrong-assumption

**Symptom.** The human picked a different exported folder in the Send tab, tapped Start, and
the report shown afterward never changed — always the first trip's result.

**Root cause.** `LoopModel` persisted `tripID`/`jobID` in two flat `UserDefaults` keys, not
scoped by folder. Picking folder B after a successful run on folder A left `tripID`/`jobID` set
from A. `resolveTrip` returned the stale `tripID` unconditionally (`if let tripID { return
tripID }`), so folder B's assets got negotiated into folder A's trip. `resolveJob` was worse: `if
let jobID { ...; return jobID }` reused A's job **without checking whether it had already
finished** — so the app skipped starting a build entirely and re-fetched A's already-succeeded
report.

**Fix.** Persist a `[folderPath: {tripID, jobID}]` record instead of two flat keys, reloaded on
every folder selection. `resolveJob` now fetches the job's real `state` before reusing it and
only reuses when it is `queued`/`running`/unknown — `succeeded`/`failed` triggers a fresh build.

**Lesson.** *"Persisted so a relaunch resumes" was the comment on the field that caused this* —
the intent (survive a kill mid-run) was real and correct, but the implementation generalized it
to "always reuse," which silently conflated two different situations: resuming an interrupted
run, and starting again after a finished one. Two states that must never be confused shared one
variable with no state check between them.

**Gap this exposed.** `LoopModel` has no automated test coverage at all — it lives inside `#if
os(iOS)` in `StoryApp`, and `StoryAppTests` only runs hostlessly via `swift test`, which never
executes iOS-gated code. The 214-test hostless suite was green before this bug shipped and green
after the fix, having exercised none of the code in question either time. A bug in an untested
module was found by a human's finger, not by CI — which is the argument for eventually wiring an
on-device (or at least a testable, hostless-extracted) path for this logic, not just a note that
it happened to work out this time.
