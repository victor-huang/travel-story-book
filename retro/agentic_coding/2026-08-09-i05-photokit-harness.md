# Wave 0 / I05 — getting PhotoKit under test

One task, most of a session. Every entry below is an environment or wrong-assumption failure;
none of them were bugs in the code being written, which is what made them expensive.

---

### A SwiftPM test target can never authorize PhotoKit

**Cycle:** Wave 0 / I05
**Cost:** ~20 min, two hung runs
**Category:** wrong-assumption

**Symptom.** `PHPhotoLibrary.requestAuthorization` under `swift test` on a simulator:

```
[access] Created Error: Error Domain=kTCCErrorDomain Code=2 "server error: Unable to
construct an identity to kTCCServicePhotos, ... identifier=com.apple.xctest"
```

**Root cause.** I read this as a missing grant and tried `simctl privacy grant photos
com.apple.xctest`, which reported success and changed nothing. The message says what is
actually wrong: TCC attributes every request to a **bundle identity**, and a bare `xctest`
process has none. There is no entity to grant permission *to*. A physical device would not have
helped — the same TCC applies.

**Fix.** `ios/StoryBookHost.xcodeproj` with a minimal `HostApp`, hosting `PhotoExportTests`.
Tracker D10. `StoryKit` and `StoryService` still run hostless in 0.008 s; only `PhotoExport`
pays for Xcode.

**Lesson.** *"Permission denied" and "there is nobody to permit" are different failures.* Read
the error text literally before reaching for the tool that fixes the error you expected.

---

### `simctl privacy grant` is ignored for Photos on iOS 26

**Cycle:** Wave 0 / I05
**Cost:** ~40 min, four full erase/install/run cycles
**Category:** environment

**Symptom.** With `kTCCServicePhotos|com.storybook.hostapp|2` verified present in the
simulator's `TCC.db`, the app still showed the consent dialog and
`PHPhotoLibrary.authorizationStatus` returned `.notDetermined`. Granting `all` instead wrote a
single `kTCCServiceAll|2` row and behaved identically.

**Root cause.** `tccd`'s own log, which is the only place this is visible:

```
tccd: Got 1 auth from db for client: Sub:{com.storybook.hostapp} ... flags: 0
tccd: AUTHREQ_PROMPTING: msgID=..., service=kTCCServicePhotos, subject=...
```

It **finds** the grant and prompts anyway. Apple's own `simctl privacy` help text advertises
exactly this workflow (`grant test host photo permissions`), so the documented path is simply
not honoured on Xcode 26.3 / iOS 26.

**Fix.** A human clicks *Allow Full Access* once per simulator; the decision persists and later
runs take 5 s (suite: 0.187 s). Recorded as D11, with open question 10 for automating it via
XCUITest. CI runs `swift test` only — the export path has no CI coverage today, and saying so is
better than a green tick that means nothing.

**Lesson.** *When a documented mechanism silently does nothing, the daemon's log is evidence and
the database row is not.* I checked `TCC.db`, saw the row, and concluded the grant was in
effect — twice. The row records intent; only `tccd` records the decision.

---

### Killing a test run parked on a simulator dialog poisons the device

**Cycle:** Wave 0 / I05
**Cost:** ~15 min of retries that could not have worked
**Category:** environment

**Symptom.** After `pkill`ing a hung `xcodebuild`, every later run hung identically — including
runs whose code and permissions had genuinely been fixed.

**Root cause.** The consent alert from the killed process was never dismissed, so SpringBoard's
modal queue kept holding a dialog belonging to a dead PID. New requests queued behind a ghost.
Proof it was wedged rather than waiting: two screenshots ten minutes apart were **byte-identical**
(same MD5). A live screen awaiting input still changes.

**Fix.** `simctl shutdown && simctl erase` between attempts. Nothing less clears it.

**Lesson.** *A retry against dirty state is not a retry.* And **screenshot-diffing is a cheap
liveness probe** — comparing two MD5s separated by 30 s distinguishes "slow" from "stuck" without
any instrumentation.

---

### A hang and slow work are indistinguishable without a timeout

**Cycle:** Wave 0 / I05
**Cost:** ~15 min across two runs
**Category:** tooling

**Symptom.** Two runs sat for 5+ minutes each. I described them to the user as slow. They were
not slow; they were never going to finish.

**Root cause.** `xcodebuild test` has no default per-test timeout, so a blocked test is
indistinguishable from a long one. I compounded it by *inferring* progress instead of observing
it, and told the user a confident and wrong story about grant ordering — the grants were
present the whole time, with an mtime predating my "fix".

**Fix.** `-test-timeouts-enabled YES -maximum-test-execution-time-allowance 120` on every
invocation. The next failure took 120 s instead of forever and named the four blocked tests.

**Lesson.** *Make the environment fail fast before debugging inside it.* Also: exit code 0 from
that run still meant `** TEST EXECUTE FAILED **` in the log — **read the log, not the status.**

---

### The test suite wrote eleven files into the developer's real photo library

**Cycle:** Wave 0 / I05
**Cost:** near-miss; minutes to detect, but it was already on disk
**Category:** near-miss

**Symptom.** After the simulator suite went green, `swift test` on the mac host reported **32
tests passed** — including four that need a photo library. `~/Pictures/Photos
Library.photoslibrary` had been modified two minutes earlier and contained 11 assets and a
`storybook-test-fixtures` album.

**Root cause.** Rewriting `LibraryFixtures` to add the device fixtures dropped the `#if
os(iOS)` guard and the `.enabled(if:)` conditions along with it. On macOS `PHPhotoLibrary` is
not absent — it is the developer's own. The tests then **passed**, which is precisely why
nothing flagged it.

**Fix.** Guarded twice: the tests gate on `LibraryFixtures.canSeedLibrary`, and `seedIfNeeded`
throws `refusingToSeedHostLibrary` regardless, because a guard living only in the caller is one
refactor from gone. Verified by counting assets before and after a run (11 → 11) rather than by
rereading the code.

**Lesson.** *A passing test that touches a real user resource is worse than a failing one.* This
project's non-destructive guarantee is written about the traveller's source tree; it applies
just as much to the machine the suite runs on. When a rewrite removes a conditional, ask what
that conditional was protecting — the answer here was "someone's photographs".

---

### Sanitising a video's GPS is not done when the tags read clean

**Cycle:** Wave 0 / I05
**Cost:** ~10 min; would have been permanent and public
**Category:** near-miss

**Symptom.** After rewriting `Keys:GPSCoordinates` with `exiftool`, every metadata reader showed
Salzburg. A raw-byte grep still found the original ISO 6709 string — the capture location, which
is the author's home — at offset 15,967,521.

**Root cause.** iPhone clips record per-frame GPS into the `mebx` timed-metadata **track
samples**, so the original string lives in `mdat`, which container-level metadata edits never
touch. Dropping those tracks was not an option: they are the reason the fixture exists.

**Fix.** Patched the string in place. An ISO 6709 coordinate at that precision is a fixed 26
bytes, and the replacement (`+47.7949+013.0475+540.000/`) is the same width, so no atom offset
moved; `apac` and all six `mebx` tracks survive and the file still decodes. Verified by
searching for text *and* IEEE-754 float and double encodings of the old coordinates, in both
endiannesses.

**Lesson.** *For anything heading to a public repo, grep the bytes, not the tags.* A metadata
reader shows you the metadata layer; media files carry the same fact in more than one layer.
Related: an `ffmpeg -c copy` trim shrank the clip from 19 MB to 3.6 MB and kept `apac`, but
re-tagged the six `mebx` tracks as `stts` — the cheap size win would have quietly destroyed the
property the fixture was captured for.

---

### `generate.py` would have deleted the irreplaceable fixtures

**Cycle:** Wave 0 / I05
**Cost:** none — caught before running it
**Category:** near-miss

**Symptom.** The two device captures were uploaded into `tests/fixtures/media/`.
`tests/fixtures/generate.py:187` begins `shutil.rmtree(MEDIA_DIR)`.

**Root cause.** That directory is owned by a deterministic generator, and `uv run python
tests/fixtures/generate.py` is a documented command in CLAUDE.md. No generator can produce a
spatial-audio track, so the loss would have been permanent.

**Fix.** New `tests/fixtures/device_media/`, with a README stating what each file proves, why it
is not in `media/`, and that the coordinates are deliberately not the capture location.

**Lesson.** *Regenerable and non-regenerable artifacts must not share a directory when something
rmtree's it.* Before putting a file somewhere, ask what already claims ownership of that path.
