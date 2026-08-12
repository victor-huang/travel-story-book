# S07 — reel endpoints

**Cycle:** Wave S / S07
**Cost:** ~15 min re-deriving reel.json's real shape; caught before it shipped, not after
**Category:** wrong-assumption, near-miss

### `reel.json` has no `aspect`, `day` or `name` field at top level

**Symptom.** First draft of the test suite asserted `reel_json["aspect"]`, `reel_json["day"]`,
`reel_json["name"]` — all `KeyError`.

**Root cause.** I designed the test against what I *expected* `reel.json` to record (the request
options, since the task description says "reel.json ... stays the honest record of what a render
actually did"), instead of reading `write_reel_json` in `src/story_book/export/reel.py` first. The
real schema nests everything under `video`/`audio`/`video_sources`/`excerpts`/`subtitles`, and it
records **outcomes** (the file's own `aspect`, whether music was mixed in) rather than **inputs**
(there is no `day` or `name` key at all — a day-only or named render is distinguished only by its
*filename*, via `reel_filenames(selection.slug)`).

**Fix.** Read `write_reel_json` before writing the assertions, then fixed the tests to check
`reel_json["video"]["aspect"]`, `reel_json["video"]["file"]` (for the day/name cases — the slug is
the recorded proof, not a duplicated field), and `reel_json["audio"]["music_supplied"]`.

**Lesson.** "The artifact records what a render actually did" is a design *rule*, not a promise
that every input the client sent has a same-named output field. When a service wraps a CLI and
tests the CLI's own manifest, read the manifest writer before writing the assertion — the two are
easy to conflate because they're about the same nouns (aspect, day, name) but are not the same
contract (request vs. outcome).

### A shared per-tick heartbeat would have corrupted a build's own progress phase

**Symptom.** None yet — caught while writing the code, not by a failing test.

**Root cause.** `Worker._wait_with_heartbeat` called `self.index.heartbeat_job(phase="build", ...)`
on every tick while a subprocess ran. Adding `_reel`, the natural move was to reuse this loop
verbatim for both `_build` and the new `_reel`. Done that way, the *first* heartbeat tick during a
reel job would have overwritten `job.phase` from whatever `_execute` had just set — but the same
loop, unchanged, would then also fire during a **build**, still writing `phase="build"` literally,
so nothing would break for build. The actual danger was subtler and easy to miss on a quick read:
the loop is one method shared by both callers, so its hardcoded `phase="build"` string is silently
wrong for a reel the moment it's reused, and the fix has to be a parameter, not a rename.

**Fix.** `_run_cli` and `_wait_with_heartbeat` both take a `phase` argument now; `_build` passes
`"build"`, `_reel` passes `"render"`. `jobs.py`'s `_job_json` branches on `job.kind`, not `job.phase`
alone, as a second line of defense — but the phase string is still what `heartbeat_job` persists,
and a wrong one there would have been a real, if minor, misreport (a reel job's own poll showing
`phase: "build"` briefly) rather than a crash, which is exactly the kind of thing that survives a
quick test run and gets found by someone reading the response three weeks later.

**Lesson.** When two callers are about to share a loop that writes shared mutable state
(`job.phase` on a row both `_build` and `_reel` write to), grep for every hardcoded string the loop
currently writes before extracting it — "this constant looks caller-specific" is the tell, and it
is cheaper to check before the extraction than to debug a stale phase after.

### A hardcoded loopback URL in a test can hit a real, different server

**Symptom.** Local-object-store tests initially issued `httpx.put("http://127.0.0.1:8000/...")`
directly. On this shared dev machine, `lsof -iTCP:8000` shows a *real* `storybook-service`
already running (per this tracker's own log of device testing sessions) — so the PUT returned a
real `200`, against a completely different process's object store than the one the test's own
`Settings`/`LocalFileObjectStore` pointed at. The subsequent build then failed with "declared
assets have not been uploaded", because the bytes went into someone else's directory.

**Fix.** Route every "presigned" PUT/GET for the local backend through the same `TestClient` the
app itself is being tested with, never a real socket to a fixed port — the local backend's whole
premise ("a filesystem masquerading as S3") only holds if the "presigned" URL is resolved the same
way the app resolves it, in-process.

**Lesson.** A test that issues real network I/O to `127.0.0.1:<fixed port>` is not isolated from
whatever else is listening on that port on the machine running it — true of any dev box, and this
project's own log already has one instance of an agent finding (and killing) an unrelated process
squatting on a port it assumed was free. Prefer the in-process client over a real socket whenever
the code under test is a router in the same app being tested.
