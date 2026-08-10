# iOS Implementation Tracker

Companion to the three iOS design docs. They are **what and why**; this doc is **who, in what
order, and how not to collide** — the source of truth for iOS progress.

| Doc | Role |
| --- | --- |
| [`ios_client_app.md`](./ios_client_app.md) | The app: what it is, curation, privacy, milestones |
| [`ios_client_implementation.md`](./ios_client_implementation.md) | Language, module layout, task order, testing |
| [`ios_backend_service.md`](./ios_backend_service.md) | Ingest, jobs, storage, retention, delivery |

If a design doc and this doc disagree, the design doc wins on design and this doc wins on status.
Amend the design doc rather than diverging from it in code.

**This tracker does not cover the Python pipeline.** That is
[`implementation_tracker.md`](./implementation_tracker.md), and it is still the owner of every
file under `src/story_book/`. A few tasks here need a Python change; they go through *that*
tracker's cross-task request table. See [Changes to the Python side](#changes-to-the-python-side).

---

# How multiple agents work here

## The one rule that prevents most damage

**One task, one agent, one disjoint set of files.** Every task lists the files it **owns**. You
may *read* anything; you may *write* only files your task owns. If your task needs a change to a
file another task owns, do not edit it — record it under
[Cross-task requests](#cross-task-requests) and keep going on what you can.

## Waves

**Wave 0 is serial and blocking.** It freezes the Swift package layout and the three types every
other target compiles against. Nothing in Wave 1+ starts until Wave 0 is merged.

Within a wave, tasks are parallel-safe by construction. Between waves, respect `Depends on`.

Waves map onto the milestones in the design docs: Wave 1 is M0, Wave 2 is M1, Wave 3 is M2, and
so on. **Wave 3 (the reel) is the priority after M1** — decided 2026-08-09, see
[Decisions](#decisions).

## Claiming a task

1. Check `Status` is `todo` and every `Depends on` task is `done`.
2. Edit this file: set `Status` to `wip` and put your agent name/ID in `Owner`. Make that edit
   **before** writing any code — it is the lock.
3. Work only inside your owned files.
4. When finished: tests pass, `Status` → `review` (or `done` if you also verified the acceptance
   criterion against a real device), and add a line to the [Log](#log).

If a task is already `wip` with an owner, pick another. Never take over a `wip` task without the
human's say-so.

## Definition of done for any task

- The acceptance criterion in the task entry is demonstrably met.
- Tests exist and pass (see [Testing rules](#testing-rules)).
- `swift build` and `swift test` are clean for the touched targets.
- No file outside your owned set was modified.
- The [Log](#log) has your entry.

## Escalate to the human, don't improvise

Stop and ask if you hit any of these:

- A Wave 0 contract needs to change.
- A design doc is wrong, ambiguous, or silent on something load-bearing.
- A task needs a paid developer account, a provisioning profile, or a physical device you do
  not have.
- Your acceptance criterion cannot be met without a real photo library you do not have.
- You are about to write to anything under `src/story_book/` without a resolved cross-task
  request in the Python tracker.

## After each dev/test cycle

Add a retro entry to `retro/agentic_coding/` for anything that cost real time or shipped a wrong
result — above all **tests that were wrong rather than code**. The iOS work has a specific
version of that hazard: a suite that passes on the simulator and proves nothing, because the
simulator library has no HEIC-with-GPS-and-offset, no burst, no Live Photo, and no spatial-audio
`.mov`.

---

# Repo layout (frozen by Wave 0)

The app lives in **this repository** under `ios/`. The contract, the fixtures and the parity
harness are all here, and a second repo means the app and the format it targets drift
independently. Split later if it ever earns its own release cycle.

```
ios/
  Package.swift                                  I01
  Sources/
    StoryKit/            pure logic. no UIKit, no SwiftUI, no PhotoKit
      ContentHash.swift                          I02
      TripDocument.swift                         I03
      ExportLedger.swift                         I04
      Sidecar.swift                              I04
      OverridesFile.swift                        I43
    PhotoExport/         PhotoKit + ImageIO + AVFoundation
      LibraryScope.swift                         I10
      ResourceSelection.swift                    I13
      StillExporter.swift                        I11
      ClipExporter.swift                         I12
      HomeFilter.swift                           I14
      FolderWriter.swift                         I15
    StoryService/        URLSession client
      Auth.swift                                 I23
      NegotiateClient.swift                      I20
      UploadQueue.swift                          I21
      JobPoller.swift                            I22
      MediaCache.swift                           I33
    StoryApp/            SwiftUI
      AppShell.swift, TripList.swift             I24
      ReportWebView.swift, AssetSchemeHandler.swift  I25
      ReelOptions.swift, MusicImport.swift       I30, I31
      Player.swift, ShareSheet.swift             I32, I34
      ProposalGrid.swift, Alternates.swift       I40, I41, I42
  Tests/
    StoryKitTests/                               per owning task
    PhotoExportTests/                            per owning task
    StoryServiceTests/                           per owning task
tests/ios_parity/                                I16   (Python side)
.github/workflows/ios.yml                        I01
```

`StoryKit` and `PhotoExport` hold everything that can be wrong in a way a test can catch.
`StoryApp` stays thin enough that no logic hides in a view.

---

# Testing rules

The house rules apply unchanged: **one behaviour per test**, real fixtures over mocks for
anything touching media, and **a test of a failure mode must be shown to fail**.

Three additions specific to this half:

- **The simulator is not enough for the export path.** Either seed a library via
  `PHPhotoLibrary` from the committed fixtures in `tests/fixtures/media/`, or run export tests on
  a device. I05 decides which, once, for everyone.
- **Contract tests run on the Python side**, because that is where the other half of the contract
  lives: hash agreement, overrides round-trip, `trip.json` decoding.
- **Assert identity, not presence.** P06 found nine assets declared `kind: "video"` whose
  exported files were JPEGs under `.mov` names, past a schema check and 87 passing tests. For
  every declared media type, verify one file's actual bytes.

---

# Decisions

Taken 2026-08-09 with the human. Recorded here because they change task scope.

| # | Decision | Consequence |
| --- | --- | --- |
| D1 | **Swift 6 + SwiftUI, native, minimum iOS 17.** | Every hard part of this app is an Apple framework call, and stripping EXIF on resize is the default behaviour of most cross-platform image pipelines. No shared-code upside: the pipeline stays Python. |
| D2 | **Trip context is deferred. `context.supplied` stays `false`.** | No context-collection UI in any wave. The model may infer context from the photographs, but inferred context must never set `supplied: true` — an artifact never overstates its contents. |
| D3 | **The book is the existing HTML report in a webview.** | No native re-render of `report.py`. One renderer, no drift. Wave 2 owns the webview. |
| D4 | **Report images resolve to the phone's originals** via a custom scheme, falling back to the server preview. | Better than a download *and* better quality — the server only ever sees 1080px. Forces the export ledger into Wave 1 (see D6). |
| D5 | **Posters and reels are cached locally on first access.** | Both artifacts are immutable, so the cache needs eviction but no invalidation. |
| D6 | **`ContentHash` moves from M1 to Wave 0.** | `asset_id` is a prefix of the BLAKE2b of the *exported* bytes, so only the phone at export time knows which `PHAsset` produced which hash. If Wave 1 does not record it, it cannot be rebuilt without re-exporting the whole library. |
| D7 | **Reel first after M1.** | Wave 3 outranks Wave 4. Consequence: the music picker is on the critical path, and it is the one screen that will feel broken if done late. |
| D8 | **Sign-in with Apple and Google; the service indexes trips and reels.** | Wave 2 owns auth. In-app account deletion is an App Store requirement and collides with indefinite metadata retention — unresolved, see [Open questions](#open-questions). |
| D9 | **Seeded simulator for the export suite** (decided 2026-08-09, I05). Tests seed a simulator photo library from the committed fixtures via `PHAssetCreationRequest` (`LibraryFixtures.swift`). | Delivered. Both prerequisites are met: Xcode 26.3 is installed, and two real captures live in `tests/fixtures/device_media/`. **Not CI-runnable** — see D11. |
| D10 | **An Xcode project hosts the PhotoExport tests** — `ios/StoryBookHost.xcodeproj` plus a minimal `HostApp`. Amends I01's "Swift Package, no Xcode project". Approved by the human 2026-08-09. | PhotoKit authorization goes through TCC, and TCC attributes a request to a *bundle identity*. A bare SwiftPM test target has none, so `requestAuthorization` fails with `unable to construct an identity to kTCCServicePhotos` — nothing to grant *to*, which no `simctl privacy grant` can fix, and a physical device would not have dodged it either. `StoryKit` and `StoryService` still test hostless under `swift test` in 0.008 s; only `PhotoExport` pays the Xcode cost. |
| D13 | **`Package.swift` gains a `StoryAppTests` target and a `StoryApp` library product.** Amends I01. Approved by the human 2026-08-09. | Every Wave 2 task says "+ tests" and there was nowhere to put them — three test targets existed, none for `StoryApp`. Additive only: no existing target, dependency or platform changes. The product exists for the same reason the other three do, so the Xcode host can link it. |
| D12 | **Exporters read an `ExportSource`: a `PHAsset` or a local file URL.** Decided 2026-08-09 with the human, answering open question 11. | `PHAsset` is the production path and stays first-class — it is the only source of `creationDate`, `location`, burst ids and Live Photo pairing. But the metadata-preserving core (ImageIO downscale, AVFoundation export) takes *bytes*, so the same code exports a committed fixture file with no library and no authorization. Consequence: the riskiest work in the project gets CI coverage despite D11, and I16 can drive both halves through one API. Resource selection collapses to "the file" for the file case. |
| D11 | **`PhotoExportTests` runs locally, not in CI, and needs one human click per simulator.** | On Xcode 26.3 / iOS 26, `simctl privacy grant photos` is **not honoured**: `tccd` logs `Got 1 auth from db for client` and then `AUTHREQ_PROMPTING` anyway, for `photos` and for `all` alike. In a headless run that dialog blocks forever. Once a human clicks *Allow Full Access* once, the decision persists and later runs take 5 s. Until that is automated (XCUITest tapping SpringBoard is the known route), CI runs `swift test` only. **Always pass `-test-timeouts-enabled YES`** so a stuck dialog fails in seconds instead of hanging. |

---

# Wave 0 — Contracts (SERIAL, blocks everything)

One agent does all of Wave 0, in order. Do not parallelize this.

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| I01 | Package skeleton, four targets, CI | review | claude (2026-08-09) | — |
| I02 | `ContentHash` — BLAKE2b, chunked | review | claude (2026-08-09) | I01 |
| I03 | `TripDocument` — decode `trip.json` | review | claude (2026-08-09) | I01 |
| I04 | `ExportLedger` + `Sidecar` formats | review | claude (2026-08-09) | I02 |
| I05 | Test harness: fixtures on device or seeded simulator | done | claude (2026-08-09) | I01 |

### I01 — Package skeleton, four targets, CI
**Owns:** `ios/Package.swift`, all target directories with a placeholder, `.github/workflows/ios.yml`
Swift 6 with strict concurrency on, minimum iOS 17. Four targets per the layout above; `StoryKit`
and `PhotoExport` must build and test without an app scheme, which is the argument for a Swift
Package over an Xcode project. CI on macOS only — the existing Python jobs are untouched. **Pin
actions to exact versions**; `releases/latest` tells you the version, not which refs resolve.
**Done when:** `swift build && swift test` passes on an empty suite, in CI, on macOS.

### I02 — `ContentHash` — BLAKE2b, chunked
**Owns:** `ios/Sources/StoryKit/ContentHash.swift`, `ios/Tests/StoryKitTests/ContentHashTests.swift`
`hashlib.blake2b()` with default parameters is a 64-byte digest, hex-encoded to 128 characters
(`src/story_book/pipeline/scan.py:45`), and `asset_id` is a prefix of it. Reproduce that digest
byte for byte or hash negotiation silently matches nothing and every asset re-uploads. **CryptoKit
does not provide BLAKE2b**; confirm the same for `swift-crypto` before choosing between a vendored
Swift implementation and libb2 through C interop. Chunked, so a 400 MB clip is not held in memory.
**Done when:** a committed fixture hashed by `ContentHash` and by `hashlib.blake2b` produces the
same 128 characters, asserted in CI on both sides rather than by inspection.

### I03 — `TripDocument` — decode `trip.json`
**Owns:** `ios/Sources/StoryKit/TripDocument.swift`, `ios/Tests/StoryKitTests/TripDocumentTests.swift`
A read-only view of the published contract, decoding against `src/story_book/trip_schema.json`.
Everything the app displays comes from here: `assets` with `thumbnail`/`preview`/`quality`/
`cluster`/`selected`, `days`, `trip_highlights`, `privacy`, `context`. **Decode `cluster` fully** —
`{id, kind, is_keeper}` is what makes the alternates strip possible without a schema change.
**The app computes nothing.** No days, no events, no selection. A story is an overlay, never a
source of structure, and the same rule governs the app.
**Done when:** the committed `trip.json` from the real trip decodes with no unknown-field loss,
and a schema change breaks this test in CI rather than in someone's hands.

### I04 — `ExportLedger` + `Sidecar` formats
**Owns:** `ios/Sources/StoryKit/ExportLedger.swift`, `ios/Sources/StoryKit/Sidecar.swift`, tests for both
Two records written at export time, and the reason both must exist before Wave 1 starts:

- **`Sidecar`** — per-asset JSON uploaded beside the media: `creationDate`, `location`, and the
  *source* of each. `PHAsset` hands over an absolute timestamp and coordinates directly, so the
  server can prefer it over EXIF inference — but it must record which source won. Timezone
  resolution is the highest-risk logic in this project and it is not to be made ambiguous.
- **`ExportLedger`** — local, durable, maps `PHAsset.localIdentifier` ↔ content hash ↔
  `originalFilename`. This is the only moment anything knows that mapping (D6). It is what lets
  the report resolve an `asset_id` back to the original on the phone.

**Done when:** both round-trip through encode/decode, and the ledger survives an app relaunch.

### I05 — Test harness: fixtures on device or seeded simulator
**Owns:** `ios/Tests/PhotoExportTests/LibraryFixtures.swift`, a short decision note in this file
Decide once, for every later task: seed a `PHPhotoLibrary` from the 27 committed fixtures in
`tests/fixtures/media/`, or run export tests on a physical device. Deciding early avoids a suite
that passes everywhere and proves nothing — the same failure as HEIC decoding "working" for
months because the test registered the opener itself and the app never did.
**Done when:** a test can obtain a `PHAsset` for the HEIC+GPS+offset fixture and for the
spatial-audio `.mov`, and the decision is written into [Decisions](#decisions).

---

# Wave 1 — Export (M0, parallel, ~5 agents)

**M0 needs no backend, no account, and no queue, and it proves the only claim the rest depends
on.** It is shippable alone for anyone with a laptop.

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| I10 | `LibraryScope` — authorization and scope selection | done | claude/I24 agent (2026-08-09) | Wave 0 |
| I11 | `StillExporter` — 1080px, properties copied | review | claude (2026-08-09) | I10, I13 |
| I12 | `ClipExporter` — 1080p, metadata carried | review | claude (2026-08-09) | I10, I13 |
| I13 | `ResourceSelection` — which resource to export | review | claude (2026-08-09) | I10 |
| I14 | `HomeFilter` — exclusion before upload | review | claude (2026-08-09) | I10 |
| I15 | `FolderWriter` — the source folder, and hand-off | wip | claude — unrecorded agent, see log | I11, I12, I14 |
| I16 | **Parity harness** — the M0 gate | todo | — | I15 |

I11 and I12 are where the project's risk is concentrated, and they are testable long before there
is an app around them. Start there.

### I10 — `LibraryScope`
**Owns:** `ios/Sources/PhotoExport/LibraryScope.swift` + tests
`PHAsset` via **full library read authorization, not `PHPickerViewController` alone** — the
pipeline wants `creationDate`, `location`, burst identifiers and Live Photo pairing, and those are
properties of the asset in the library, not of a file handed out by a picker. Three low-effort
scopes: date range, album or Photos "Trip" memory, and multi-select drag across the grid.
Also the **cull check**: compare the selected count against how many assets exist inside the range
the selection covers, and nudge when the set has been thinned. Event detection segments on time
and location gaps and dedup clusters bursts; both read the stream, not the highlights. A set
reduced to favourites comes back with gappy events and nothing to select from, and the user will
have done the tiring work *and* got a worse result. When in doubt, include it.
**Done when:** each scope yields the expected `[PHAsset]` against the seeded library, and a
thinned selection triggers the nudge while a lightly-pruned one does not.

**Done 2026-08-09**, both halves against the criterion: 12 tests in CI (fetch options, and the
cull check, which is arithmetic over two counts) and **6 on the iPhone 17 Pro simulator against
real `PHAsset`s** — the D11 permission grant persisted from I05, so this needed no human click and
took 1.8 s. Every scope has a control that must fail: a date range that finds the fixture is
paired with one that finds none, so the predicate is shown to be able to exclude.

**The cull threshold is still unset, and the code says so.** Open question 5 asks for it to be set
by watching real selections; none have been watched. What the human did give is two anchors — 60
of 800 nudges, 600 of 800 does not — so `CullCheck` carries those as constants, a placeholder
threshold inside the band they admit, and a test asserting that **every** threshold in that band
separates them. The band is the claim; the number is not, and replacing it with a measured value
changes one line. Two further consequences of "never emit a measurement you did not take":
`keptFraction` is `nil` rather than `0.0` when there was nothing to divide by, and a selection
with no comparable range reads `.noEvidence` rather than `.fine` — a `.limited` grant sees only
what it was shown, and must not be told its selection is healthy on the strength of that.

### I11 — `StillExporter`
**Owns:** `ios/Sources/PhotoExport/StillExporter.swift` + tests
`PHImageManager`'s resized-image requests return a `UIImage` with **no EXIF at all**. The correct
path is `requestImageDataAndOrientation` for the original data, then downscale through ImageIO,
copying the source's properties into the `CGImageDestination`. Fields that must survive, and who
consumes them: `DateTimeOriginal` (every day and event boundary), `OffsetTimeOriginal` (first
branch of timezone resolution), GPS lat/lon (geocode, home filter, map, `gps_backfill`),
Orientation (`geometry.orientation`, reel framing), Make/Model (the same-device neighbour rule).
Stream per asset; never hold a batch of full-size images — this is where an app gets jetsam-killed.
**Done when:** every field above survives a 1080px export of the HEIC+GPS+offset fixture, verified
by reading the output with the pipeline's own metadata code.

### I12 — `ClipExporter`
**Owns:** `ios/Sources/PhotoExport/ClipExporter.swift` + tests
`AVAssetExportSession` at `AVAssetExportPreset1920x1080` does **not** carry QuickTime creation date
or location unless set explicitly. The fixture set already contains a Photos-export-shaped `.mov`
whose `CreateDate` disagrees with its `Keys:CreationDate`; that disagreement is a property of how
an exporter writes the file, so **assume an AVFoundation exporter produces its own variant and do
not assume it matches Photos**. Record what it actually writes.
A modern iPhone also writes spatial audio as a second `apac` track plus `mebx` metadata streams —
known from the Python side, where mapping every audio stream broke 58 of 69 real clips. Decide
deliberately what the export carries.
**Done when:** creation date and location survive on a device-captured clip, and the exported
file's actual bytes are verified to be a video (`file -b`), not merely present under a `.mov` name.

### I13 — `ResourceSelection`
**Owns:** `ios/Sources/PhotoExport/ResourceSelection.swift` + tests
One type both exporters call, answering "which `PHAssetResource` do I actually export?" for Live
Photos, slow-mo, cinematic mode, RAW+JPEG pairs, and edited-vs-original versions. Each has an
answer, and **getting one wrong produces a file that looks fine and carries the wrong timestamps**.
Enumerate them explicitly rather than discovering them. Also owns filename resolution:
**preserve `PHAssetResource.originalFilename`**, because `overrides.toml` addresses media by
filename or `asset_id` (`src/story_book/overrides.py:205`) and `IMG_1815.mov` staying
`IMG_1815.mov` is what lets corrections carry across from the laptop workflow.
**Done when:** every variant above has a test naming the resource chosen and why, including at
least one asset not resident on device (iCloud "Optimize iPhone Storage").

### I14 — `HomeFilter`
**Owns:** `ios/Sources/PhotoExport/HomeFilter.swift` + tests
`home_filter` runs before *export*, which is after ingestion — fine when ingestion is a local
folder, useless once bytes have left the device. **The exclusion applies before upload and
excluded media is never transmitted.** The coordinates are on the phone, so this is easy; it just
has to be a decision rather than an oversight. This is the only place the hosted model is
materially weaker than the laptop one.
**Done when:** an asset within `home_exclusion_km` of the configured home is absent from the
export folder and absent from the ledger, proven by a test that fails when the filter is disabled.

### I15 — `FolderWriter`
**Owns:** `ios/Sources/PhotoExport/FolderWriter.swift` + tests
Writes the source folder the CLI already accepts, plus the sidecars, and populates the
`ExportLedger` as it goes. Hand-off via Files / AirDrop / iCloud Drive. Must be resumable across
many short foreground sessions: `BGProcessingTask` is opportunistic with no guarantee and jetsam
kills on memory, and the pipeline's own resumability does not help here — this is the phone's half.
**Done when:** `uv run story-book build` on the produced folder completes, and killing the export
midway and restarting produces the same folder without re-exporting what was already written.

### I16 — Parity harness *(Python side)*
**Owns:** `tests/ios_parity/` (new directory)
**This is M0's whole point, and the one test that cannot be replaced by anything cheaper**, because
the entire argument for this architecture is *the app produces the same shape of file Photos does*.
If it holds, the 1700 existing tests already cover what happens next.

1. Export a set of assets from the app, and the *same* assets from Photos on the Mac at 1080px.
2. Compare **field by field, justifying every difference.** Not "the file has EXIF"; not "the
   build succeeded".
3. **Read both sides with the pipeline's own metadata code**, not an ad-hoc parser — the question
   is not what `exiftool` sees but what `MetadataStage` concludes, and those differ wherever the
   pipeline resolves rather than reads.
4. **Then run the pipeline on both sets and diff `trip.json`.** Asset ids *will* differ because the
   bytes differ, so compare structure, not identity: day count and boundaries, event count,
   timestamps, resolved timezone offsets, places. A difference there is the export losing something
   a field-level diff let through.

iOS-exported fixtures join `tests/fixtures/media/` as a second producer of the same contract.
**Done when:** the field-by-field diff has a written justification for every difference, and the
`trip.json` structural diff is empty.

---

# Wave 2 — Upload, build, and the book (M1)

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| I20 | `NegotiateClient` — hash negotiation | **blocked** | — | I02, service M1 |
| I21 | `UploadQueue` — background, per-file retry | **blocked** | — | I20 |
| I22 | `JobPoller` — build progress | **blocked** | — | I20 |
| I23 | `Auth` + app shell + trip list | **blocked** | — | Wave 0, service M1 |
| I24 | Report webview | review | claude (2026-08-09) | I03 |
| I25 | `AssetSchemeHandler` — images from the phone | **blocked** | — | I04, I24, XT-1 |

**Four of the six are blocked, and not merely unstarted.** Recorded 2026-08-09:

- **There is no backend, and no task in any wave builds one.** `ios_backend_service.md` describes
  M1 (object store, queue, CLI runner) but this tracker owns no ID for it, so `service M1` is a
  dependency on something nobody has been asked to write. I20, I21 and I22 cannot start, and I23's
  criterion — *a signed-in user sees only their own trips, verified by a test with two accounts* —
  is an assertion about a service, not about the client. The client half of I23 is writable; its
  acceptance criterion is not reachable. **A task whose criterion cannot be met is not `todo`.**
- **I24 lost its `I23` dependency.** D3 says the book is the existing HTML report in a webview and
  D24 says the service delivers it as a *bundle* loaded with `loadFileURL`. Neither needs a signed-in
  user: a report directory produced by `story-book report` exercises every claim the task makes,
  and offline is the acceptance criterion anyway. Waiting for auth would have blocked the one Wave 2
  task that is fully testable today.
- **I25 waits on XT-1** — the report must be rendered with `media_rel="storyasset://"`, and
  `src/story_book/export/report.py` belongs to the Python tracker. Request filed there; not
  starting the Swift half until it resolves, because the handler's shape follows what the rendered
  HTML actually asks for.

### I20 — `NegotiateClient`
**Owns:** `ios/Sources/StoryService/NegotiateClient.swift` + tests
`POST /trips/{id}/assets:negotiate` with `[{hash, filename, size}]`, returning only what the
service lacks. Media is already keyed by BLAKE2b and re-import is a no-op, so per-file retry,
natural resume, and dedup across trips fall out for free. **Never ship a zip:** a ~600 MB blob has
no resume, blocks the server until it lands, and re-uploads everything to add ten photos.
**The upload granularity is one asset; the unit of work stays the trip.** How the bytes are
chunked must never decide what the pipeline treats as a unit — that is exactly how three one-day
`story.json` files came back from T58.
**Done when:** a second upload of an unchanged trip transfers zero bytes.

### I21 — `UploadQueue`
**Owns:** `ios/Sources/StoryService/UploadQueue.swift` + tests
Background `URLSession`, `PUT` straight to presigned object-store URLs so the API server never
proxies 600 MB, multipart for clips, per-file retry. Must survive being interrupted repeatedly and
must look intentional while doing it.
**Done when:** killing the app mid-upload and relaunching resumes without re-sending completed
assets.

### I22 — `JobPoller`
**Owns:** `ios/Sources/StoryService/JobPoller.swift` + tests
`GET /jobs/{id}` → `{state, stage, done, total}`. Progress is *real* and can be read from
`stage_result` rather than invented — do not display a fabricated percentage. Push over poll once
APNs exists (Wave 4).
**Done when:** a running build reports monotonically advancing stage counts that match the DB.

### I23 — `Auth` + app shell + trip list
**Owns:** `ios/Sources/StoryService/Auth.swift`, `ios/Sources/StoryApp/AppShell.swift`, `ios/Sources/StoryApp/TripList.swift`
Sign in with Apple and Google (D8), keychain storage, per-user auth on every route. The service
indexes trips and their reels; the app lists them. These are someone's family photographs — never
a public-read bucket, and no cross-tenant reads.
**Done when:** a signed-in user sees only their own trips, verified by a test with two accounts.

### I24 — Report webview
**Owns:** `ios/Sources/StoryApp/ReportWebView.swift` + tests
The existing HTML report in a `WKWebView` (D3). The report is a **directory**, not a file —
`index.html`, `days/<date>.html` per day, `style.css`, and `vendor/leaflet.{js,css}`
(`src/story_book/export/report.py:429-470`) — with image references pointing outside it. Per-file
signed CDN URLs break every relative link, so the service delivers the report as a bundle and the
app loads it with `loadFileURL(_:allowingReadAccessTo:)`. That also gets offline viewing for free.
**Done when:** the report renders with working day navigation and a working map, offline, after
one download.

**Delivered 2026-08-09.** 22 tests, hostless under `swift test` on the macOS host and therefore in
CI — WebKit needs no app bundle, unlike PhotoKit. Three things the task entry did not know:

- **A report bundle has four roots, not one.** `report/`, `thumbs/`, `previews/` and
  **`.cache/video/`**, the last holding video poster frames. A bundle assembled from the three
  obvious directories loses every poster silently. **What the service must ship is the reference
  closure of the report, not a directory list** — see open question 12.
- **The read-access root is the directory containing `report/`.** Passing `report/` renders every
  page perfectly with every image broken, which is the failure this project already shipped once.
  So the criterion is asserted as `img.naturalWidth > 0` — decoded pixels, the only evidence that
  bytes arrived — with a control that deletes one thumbnail and requires exactly its `<img>`s to
  fail.
- **Offline is a property of the test, not of the machine.** A `WKContentRuleList` blocking
  `^https?://` runs every rendering test with the network off, and the map test's control asserts
  that no tile decoded — if one had, "offline" was proving nothing.

### I25 — `AssetSchemeHandler`
**Owns:** `ios/Sources/StoryApp/AssetSchemeHandler.swift` + tests
Every image reference in the report goes through a `media_rel` prefix
(`export/report.py:454,472` → `templates/_shot.html:5`, `templates/index.html:32`), including the
`<a href>` that opens the full-size version. Render server-side with `media_rel="storyasset://"`
and resolve each request here — no string surgery on generated HTML, no template fork.

Resolution order, and every tier is required:

1. **Local `PHAsset`** via the `ExportLedger`. Ask `PHImageManager` for a *target size* —
   thumbnail-scale for the grid, full for the tap-through. Piping original bytes into a webview
   grid is how the app gets jetsam-killed.
2. **Server preview** from the CDN, cached locally (I33).
3. **The `no preview` placeholder** the report already renders (`templates/_shot.html:8`).

The fallback is not optional: the original may be in iCloud rather than on device, deleted, or on
another phone entirely. **Video posters have no phone equivalent** — `_derived_images` returns the
server-extracted poster frame as both thumbnail and preview for videos
(`src/story_book/pipeline/timeline.py:366-368`) — so it is a hybrid by design: stills local,
posters and reel from the CDN.
**Done when:** a report renders from the phone's originals with the network off, and degrades to
tier 2 then tier 3 in tests that force each.

---

# Wave 3 — The reel (M2) — **the priority after M1** (D7)

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| I30 | Reel options — aspect, day range | todo | — | I22 |
| I31 | Music import — Files, **not** Apple Music | todo | — | I20 |
| I32 | Playback | todo | — | I30 |
| I33 | `MediaCache` — posters and reels | todo | — | I23 |
| I34 | Share sheet | todo | — | I32 |

### I30 — Reel options
**Owns:** `ios/Sources/StoryApp/ReelOptions.swift` + tests
Aspect, music, day range, name, subtitles → `POST /trips/{id}/reel`. **Offer these while the
media is still there**: uploads are deleted one month after the most recent generation, and a
re-cut after expiry reads 720p proxies and is visibly softer. Surface the window closing — it is
only useful if the user knows about it.
**Done when:** each option reaches the service and is reflected in the returned `reel.json`.

### I31 — Music import
**Owns:** `ios/Sources/StoryApp/MusicImport.swift` + tests
**Apple Music tracks are DRM-protected and cannot be exported or mixed.** A picker showing the
subscription library produces nothing but render-time failures. Sources that work: Files/iCloud
Drive imports, purchased DRM-free downloads, a licensed catalogue. The tool ships no audio
(`docs/choosing_music.md`), so **this limitation must be legible at the picker**, not discovered
when the reel comes back silent. The track uploads as an ordinary hash-addressed asset.
**Done when:** a DRM-protected selection is refused with an explanation before upload, not after.

### I32 — Playback
**Owns:** `ios/Sources/StoryApp/Player.swift` + tests
`AVPlayer` against a short-lived signed CDN URL. Progressive MP4 streams cleanly now that the
soft-subtitle mux re-applies `+faststart` (`9eb53ab`). **`AVAssetDownloadTask` is HLS-only and
will not work here** — start a background `URLSession` download on first tap while playing from
the CDN, and use the local file on subsequent plays.
**Done when:** first play starts before the download completes, and the second play makes no
network request.

### I33 — `MediaCache`
**Owns:** `ios/Sources/StoryService/MediaCache.swift` + tests
Posters and reels, downloaded on first access and kept. **Both artifacts are immutable** — a
poster is derived from the clip's content hash, and a re-cut reel is a new `reel_id` rather than a
mutation — so the cache needs eviction but **no invalidation**: no ETags, no revalidation.
**Key by id, never by URL**: signed URLs rotate, and caching by URL re-downloads identical bytes
every rotation. For the same reason, do not lean on `URLCache`. Store in `Caches/` with
`isExcludedFromBackup`; re-downloadable content in a backed-up, non-purgeable location is a known
App Store rejection. A user-visible "keep offline" pin may move a reel to Application Support —
their choice, and undoable.
**Done when:** a rotated signed URL produces a cache hit, and a purged cache re-downloads without
error.

### I34 — Share sheet
**Owns:** `ios/Sources/StoryApp/ShareSheet.swift` + tests
Share the local reel file, not a signed URL that expires in the recipient's hands.
**Done when:** a shared reel plays for someone who is not signed in.

---

# Wave 4 — Curation (M3)

The traveller reviews **tens of items, not hundreds**, and makes **roughly twenty decisions on a
nine-day trip** — that is the budget, from the real trip: 274 assets, 64 selected, 19 hand-made
decisions that no threshold setting reproduced. **The proposal must be good enough to ship
untouched.** Curation is an improvement, never a gate: no "review 47 photos to continue".

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| I40 | Proposal grid from `trip.json` | todo | — | I03, I25 |
| I41 | `pin` / `reject` — the two primary gestures | todo | — | I40 |
| I42 | Alternates strip | todo | — | I40 |
| I43 | `OverridesFile` writer | todo | — | I41, I42 |
| I44 | Re-run and push notifications | todo | — | I43 |

### I40 — Proposal grid
**Owns:** `ios/Sources/StoryApp/ProposalGrid.swift` + tests
**Review the proposal, not the library** — the default surface is the ~50 selected items grouped
by day, not 274 and certainly not 800.
**Done when:** the grid renders from `trip.json` alone, with no client-side selection logic.

### I41 — `pin` / `reject`
**Owns:** `ios/Sources/StoryApp/Gestures.swift` + tests
One tap each, on the grid. Everything is reversible and nothing is destructive — overrides are a
file and unwinding one is an edit. **Say so in the UI**, because "reject" sounds permanent and is
not. Do not ask for titles, captions or narrative; that is the story step's job.
**Done when:** both gestures round-trip through `overrides.toml` and are individually undoable.

### I42 — Alternates strip
**Owns:** `ios/Sources/StoryApp/Alternates.swift` + tests
**The highest-leverage screen in the app.** The commonest real correction is *"right moment, wrong
frame"*, and dedup already knows the other frames: filter `assets` for `cluster.id` equal to this
keeper's `asset_id` (`timeline.py:271-288`). No schema change needed.
**Done when:** tapping an alternate writes a `keeper` override and the next build honours it.

### I43 — `OverridesFile` writer
**Owns:** `ios/Sources/StoryKit/OverridesFile.swift`, `ios/Tests/StoryKitTests/OverridesFileTests.swift`, one Python-side round-trip test
**Cluster and event ids are never written into overrides** — they are assigned fresh each run. An
event is addressed by naming a photo inside it, which is exactly what a tap gives you. A reference
matching nothing, or matching two files, is a **hard error by design**, so a generator that emits
one must be caught here rather than at the user's next build.
**Done when:** output loaded by `Overrides.load` is non-empty and fully resolvable, asserted on
the Python side.

### I44 — Re-run and push
**Owns:** `ios/Sources/StoryApp/Rerun.swift`, `ios/Sources/StoryService/Push.swift` + tests
Every expensive stage is cached, so re-running after an edit takes seconds rather than re-doing
the trip. APNs on completion — a spinner over a multi-minute render is the difference between a
good app and a bad one.
**Done when:** an override edit triggers a re-run whose changed stages are only the ones downstream
of selection.

---

# Wave 5 — Story (M4, deferred)

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| I50 | Display story prose in the book | todo | — | I24, service M4 |

Nothing in the app beyond displaying prose the service obtained. Two constraints carry over:
`context.supplied` stays `false` (D2), and **all parts go to one conversation and produce one
`story.json`** — a service that calls the API once per `package.partNofM.zip` reproduces the exact
bug T58 fixed.

---

# Changes to the Python side

These tasks need a file owned by the Python tracker. **Open a cross-task request there first.**

| Task | File | Change |
| --- | --- | --- |
| I25 | `src/story_book/export/report.py` | Make `MEDIA_REL_FROM_INDEX` / `MEDIA_REL_FROM_DAY` overridable so the report can be rendered with a `storyasset://` prefix. The templates already interpolate `media_rel`, so this is a parameter, not a template change. |
| I16 | `tests/fixtures/media/` | Add iOS-exported fixtures as a second producer of the source-folder contract. |
| I43 | — | Read-only use of `Overrides.load`; no change, but the round-trip test lives beside the Python suite. |

---

# What the app treats as frozen

Changing any of these breaks the app silently, so they go through the Python tracker's cross-task
request table like any other contract:

- the source-folder shape (1080px stills, 1080p clips, metadata intact, original filenames),
- `trip.json` and `trip_schema.json`,
- the `overrides.toml` vocabulary and its filename / `asset_id` addressing,
- `reel.json` as the honest record of what a render actually did.

---

# Cross-task requests

Need a change in a file you don't own? Add a row. The owning agent (or the human) resolves it.

| From | To (task/file) | Request | Status |
| --- | --- | --- | --- |
| XT-1 / I25 | Python tracker → `src/story_book/export/report.py` | Make `MEDIA_REL_FROM_INDEX` / `MEDIA_REL_FROM_DAY` overridable so the report can render with a `storyasset://` prefix. Filed in `implementation_tracker.md`'s table 2026-08-09. **Blocks I25.** | open |

---

# Open questions

Unresolved. Each blocks the wave named, not the whole plan.

| # | Question | Blocks |
| --- | --- | --- |
| 1 | ~~Does `swift-crypto` expose BLAKE2b, or is it vendored Swift vs. libb2 interop?~~ **Answered 2026-08-09:** neither CryptoKit nor swift-crypto exposes BLAKE2b; a vendored pure-Swift RFC 7693 implementation ships in `ContentHash.swift` (~600 MB/s after de-allocating the hot loop, digest verified against `hashlib` and the RFC vectors). | ~~I02~~ |
| 2 | ~~Device or seeded simulator for the export suite?~~ **Answered 2026-08-09:** seeded simulator — see D9. | ~~I05~~ |
| 3 | **In-app account deletion is an App Store requirement (5.1.1(v)) and contradicts keeping `story.db`, `trip.json` and previews indefinitely.** Reconcile before submission. | Ship |
| 4 | Does the service store a source tree per trip or per user? Hash-addressed storage makes cross-trip dedup free but complicates deletion — a shared asset cannot be removed with one trip. Same decision as "does the app hold a local trip list". | I20, I23 |
| 5 | Where is the cull threshold? "60 of 800 in-range" clearly warrants the nudge and "600 of 800" clearly does not. Set it by watching real selections, not by guessing. | I10 |
| 6 | Is the alternates strip built from dedup clusters alone, or also from CLIP neighbours? The two solve different problems at different thresholds, and the strip wants "another frame of this moment", not "something that looks similar". | I42 |
| 7 | Does curation re-run happen automatically on edit, or on an explicit rebuild tap? | I44 |
| 8 | Config ownership: does the app expose thresholds, or does the service pin one config and keep the knobs on the laptop path? | I30 |
| 9 | Does `ios/` stay in this repo past Wave 2? | — |
| 10 | **How does `PhotoExportTests` run unattended?** `simctl privacy grant` is ignored on Xcode 26.3 / iOS 26 (D11), so today it needs one human click per simulator. XCUITest tapping SpringBoard's *Allow Full Access* is the known route; it needs a UI-test target. Until then the export path has **no CI coverage**. | CI for Wave 1 |
| 12 | **What exactly is "the report bundle" the service delivers?** Found in I24: a rendered report reaches into four directories, and one is `.cache/video/` for video poster frames — hidden, and named for something disposable. Ship the report's *reference closure* and have the service assert it resolves before handing it over; a bundle missing posters renders blank cells and raises nothing. Also decide whether posters should move out of `.cache/` on the Python side. | service M1 |
| ~~11~~ | ~~**Should the risky half of Wave 1 avoid PhotoKit entirely?**~~ **Answered 2026-08-09: neither — both.** See D12: exporters take an `ExportSource` that is a `PHAsset` or a file URL. Original text: Metadata surviving an ImageIO downscale (I11) and an AVFoundation export (I12) can be tested against plain file URLs — no library, no authorization, runs in CI. That would leave only `LibraryScope` (I10) and `ResourceSelection` (I13) needing a real `PHAsset`. | ~~I11, I12~~ |

---

# Log

Newest first. One line per meaningful event: task claimed, task done, surprise found, decision
made.

| Date | Who | Entry |
| --- | --- | --- |
| 2026-08-09 | claude | **I10 done.** 12 tests in CI plus 6 on the simulator against real `PHAsset`s, and the simulator half needed **no human click** — I05's grant persisted exactly as D11 predicted, so the run took 1.8 s. Worth recording because D11 reads like a standing cost and is in fact a one-off per simulator. The interesting part was refusing to invent the cull threshold: open question 5 says set it by watching real selections, so `CullCheck` encodes the human's two anchors (60/800 nudges, 600/800 does not), a placeholder inside the band they admit, and a test that **every** threshold in the band separates them. A future measured value keeps the suite green; a value outside the band fails and says which anchor it broke. Same discipline twice more: `keptFraction` is `nil` rather than `0.0` when there is nothing to divide by, and a selection with no comparable range is `.noEvidence` rather than `.fine`, so a `.limited` grant is never told its selection looks healthy on no evidence. |
| 2026-08-09 | claude | **Two agents took I15 at once, and the tracker did not stop it.** I set `Status: wip` and then read the modules I15 composes before writing a line — in that window another agent wrote `FolderWriter.swift` and its tests without ever claiming the row. So the lock was held by an agent with no code and ignored by an agent with code. I stood down and moved to I10; the writer keeps it. **The claim is only a lock if it is made *and read* at the same instant, and the rule as written ("edit this file before writing any code") makes the read implicit.** The cheap fix is to re-read the row immediately before the first write, and treat a file that exists but is unclaimed as a claim. Also: neither of us could see the other, because an uncommitted claim is invisible outside its own tree — the row should be committed on its own, before the work, not with it. |
| 2026-08-09 | claude | **A Wave 1 commit swept up a Wave 2 agent's uncommitted files.** `2937135` (I13/I11) committed `ios/Package.swift` — which I01 owns and I was mid-edit on under D13 — plus a scratch probe file under `ios/Tests/StoryAppTests/`, which that task does not own. Nothing was lost and the content was correct, so this is a near-miss rather than damage. But `git commit -a` in a tree where other agents are working stages *their* work under *your* message, and the tracker's file-ownership rule cannot see it. **Commit by path, never by `-a`.** |
| 2026-08-09 | claude | **I24 done → review, and Wave 2 is mostly blocked.** Four of six tasks depend on a backend service that no task in any wave builds; recorded above rather than left as `todo`, since a task whose acceptance criterion is an assertion about a service nobody has written is not merely unstarted. I24 lost its `I23` dependency — a report bundle needs no signed-in user — and D13 adds the `StoryAppTests` target that every Wave 2 task's "+ tests" had nowhere to go into. **WKWebView runs hostless under `swift test`**, so unlike PhotoExport the book has real CI coverage. Three findings in the log below; the fourth is that the layout block at the top of this file assigns `ReportWebView.swift` to I25 and `AppShell`/`TripList` to I24, contradicting the task entries. The task entries are the detailed ones and I followed them. |
| 2026-08-09 | claude | **Three tests were wrong before any code was.** (1) A helper built a `WKWebView`, attached a local `ReportLoader` and returned only the webview; `navigationDelegate` is weak, so the loader died at the return and every link in the report went dead — images still decoded and the map still drew, so five tests passed and two failed. Fixed in the *product*: the loader now owns its webview, so the lifetime cannot be got wrong. (2) The natural control — grant read access to `report/` alone and watch every image starve — **passed run alone and failed run with its neighbours**, and `.serialized` did not save it: WebKit's file-access grants and resource cache live in one WebContent process shared by every webview, so once any earlier test loaded the media the narrow grant was unobservable. A control that only holds when it runs first is not a control; replaced with deleting one thumbnail and requiring exactly its `<img>`s to fail. (3) `standardizedFileURL` and `resolvingSymlinksInPath` cross the `/var` → `/private/var` symlink for a path that exists and not for one that does not, so a just-deleted thumbnail was reported as *outside* the bundle it was merely missing from — right file, wrong reason, and only visible because the control test existed. Containment is now component arithmetic that never touches the filesystem, tested directly. |
| 2026-08-09 | claude | **I14 done → review**, mirroring `pipeline/home_filter.py` rather than reimplementing it: same haversine, same IUGG radius, same **inclusive** boundary, same treatment of unknown locations. Parity is asserted against values produced by the Python function itself, to 1e-6 km. The rule worth not getting wrong is the second exclusion case: an item with no coordinates is excluded **only when a home is configured**. Excluding coordinate-less items unconditionally protects nothing when no home is set and silently deletes real content — a Sony body and a GoPro record no GPS — so both directions have a test. `Partition` counts unknown-location exclusions separately from near-home ones, because those items are ambiguous rather than private and the user should be able to get them back. **Note on scope:** the acceptance criterion's "absent from the export folder and absent from the ledger" is only half-provable here — the predicate and its control are tested, but the folder-level assertion belongs to I15, which owns the writer. |
| 2026-08-09 | claude | **I12 done → review, after measuring three things I had assumed.** The task says to record what an AVFoundation exporter actually writes, and doing so contradicted my own code comment. (1) Handing the asset to `AVAssetExportSession` carries the six `mebx` timed-metadata tracks **straight through** — my comment claimed they were dropped, and they hold per-frame GPS, so that was a false privacy claim in a file that had already passed review. The export now runs over an `AVMutableComposition` holding only the video and one audio track. (2) Reading the creation date as a `Date` and reformatting it silently turned `20:01:25+02:00` into `18:01:25Z`: same instant, local offset gone, and that offset is an input to the highest-risk logic in the project. The source's own timestamp string is now carried verbatim — the video half of the rule that `OffsetTimeOriginal` must survive. (3) `QuickTime:CreateDate` comes out as the *export* time while `Keys:CreationDate` holds the capture time, reproducing exactly the disagreement `clip_apple_export.mov` exists to represent; the pipeline reads the Keys value, so this is recorded rather than fought. Verified end to end: `story-book build` over the exported clip returns the same local time, the same `exif_offset` resolution, the same Salzburg place and the same `-47.1 dB` reading as the source. Also noted: the preset transcodes HEVC to H.264, which nothing had said. |
| 2026-08-09 | claude | **I11 done → review.** Every field survives a 1080px export of the real iPhone HEIC, verified twice as the task asks: with ImageIO in the Swift suite, and by running `story-book build` over the exported file, which resolved `Europe/Vienna` via `exif_offset` and geocoded Salzburg — meaning `OffsetTimeOriginal` and GPS both made it. A field-by-field `exiftool` diff of source against export is identical for `DateTimeOriginal`, `OffsetTimeOriginal`, GPS, `Orientation`, `Make`, `Model` and even `LensModel`; only the dimensions move, 4032×3024 → 1080×810, 3.4 MB → 364 KB. Two deliberate choices: orientation is preserved **as a tag, not baked into the pixels**, because the pipeline reads `Orientation` for `geometry.orientation` and rotating pixels would give a right-looking image with a wrong geometry; and the original's `PixelWidth`/`PixelHeight` are *stripped* from the copied properties, since a downscaled file claiming 4032×3024 is a measurement it cannot support. Bytes are staged through a temp file rather than held as `Data` — this is where an app gets jetsam-killed. |
| 2026-08-09 | claude | **I13 done → review.** The choice is a pure function over resource descriptors, so all ten variants are CI tests — the `Photos` *framework* is available on macOS even though a library is not, which is what makes D12 work. The subtlety worth knowing: for an edited still the bytes come from `.fullSizePhoto` but the **filename must come from `.photo`**, because the render's own filename is `FullSizeRender.heic` and `overrides.toml` addresses media by filename — shipping the render's name would silently break every correction written on the laptop. RAW+JPEG exports the processed image, a Live Photo exports the still and never the paired video. **Gap:** "at least one asset not resident on device" is only covered by asserting `isNetworkAccessAllowed`; a simulator cannot produce a genuine iCloud-offloaded asset, so that case is unproven until someone runs it against a real library. |
| 2026-08-09 | claude | **Wave 1 opened with D12, answering open question 11.** Exporters take an `ExportSource` — a `PHAsset` or a file URL — so the metadata-preserving core is CI-tested despite D11 leaving the simulator path manual. The `PHAsset` path is not left unverified by that: a simulator test asserts both sources produce the same dimensions and filename for the same picture, because a CI-green file path would otherwise prove nothing about production. |
| 2026-08-09 | claude | **I05 done.** Six tests green on an iPhone 17 Pro simulator, and the acceptance criterion is met exactly: a `PHAsset` for the HEIC+GPS+offset fixture *and* for the spatial-audio `.mov`, the latter asserted to still carry **two** audio tracks rather than merely to exist. Getting there cost most of a session and produced D10 and D11 — see `retro/agentic_coding/2026-08-09-i05-photokit-harness.md`. Three findings worth carrying: TCC needs a bundle identity, so a SwiftPM test target can never authorize; `simctl privacy grant` is silently ignored on iOS 26, and the only honest way to see that was `tccd`'s own log; and killing an `xcodebuild` run that is sitting on a simulator dialog poisons the device, so every retry after it was doomed before it started. |
| 2026-08-09 | claude | **A test suite wrote to the developer's real photo library.** Rewriting `LibraryFixtures` for the device fixtures dropped the `#if os(iOS)` guard, so one `swift test` on the mac host imported all eleven fixtures and an album into `~/Pictures/Photos Library.photoslibrary` — and **passed**, which is what made it dangerous. Now guarded twice (tests gated on `canSeedLibrary`, and `seedIfNeeded` refuses regardless), and the guard is proven by asset count before and after rather than by reading the code. A harness that can reach a real library is not a test detail; it is the non-destructive guarantee applied to our own machine. |
| 2026-08-09 | claude | **Device fixtures landed in `tests/fixtures/device_media/`, deliberately not `media/`.** `tests/fixtures/generate.py` opens with `shutil.rmtree(MEDIA_DIR)`, so a documented command would have destroyed two irreplaceable captures. Both had home GPS and this repo is public: coordinates were rewritten to Salzburg with the UTC offset moved to `+02:00` so the pair stays coherent. The video needed more than `exiftool` — iPhone clips record per-frame GPS into the `mebx` samples, so the original ISO 6709 string survived inside `mdat` while every metadata reader reported the new value. Patched in place (both strings are 26 bytes, so no atom moved). **Grep the raw bytes, not the tags.** |
| 2026-08-09 | claude | **I05 blocked, escalated.** Two facts, both needing the human: (1) this machine has Command Line Tools only, no Xcode — so neither a simulator nor a device can run PhotoKit tests here, whichever way the decision goes; (2) no committed fixture has spatial audio — `clip_speech.mov` is plain h264+aac, and an `apac` track plus `mebx` streams can only come from a real iPhone capture, so half the acceptance criterion cannot be met from `tests/fixtures/media/` at all. Recommendation recorded, not decided: seed a simulator from committed fixtures via `PHAssetCreationRequest` (the helper, `LibraryFixtures.swift`, is written and compiles), and add a small set of device-captured fixtures — spatial-audio `.mov`, Live Photo pair, burst — to cover what no generator can produce. Needs from the human: Xcode installed (or a provisioned device), and those captures. |
| 2026-08-09 | claude | **I04 done → review.** `Sidecar`: UTC instant + coordinates with a `source` beside each (`photokit_asset` / `unavailable`); absent values encode as explicit nulls, never omitted keys; never claims a timezone offset it does not have. `ExportLedger`: upsert by `localIdentifier`, `asset_id`-prefix lookup returns *every* match (byte-identical duplicates legitimately share a hash), atomic file persistence, relaunch survival proven by reloading from disk into a fresh instance. |
| 2026-08-09 | claude | **I03 done → review, with one substitution.** The committed decode fixture is a full `--no-cloud` pipeline run over `tests/fixtures/media/` (22 assets, 2 days, 3 videos, clusters, all four selection scopes), not the real trip — I do not have the real trip's photographs. Swap or add the real `trip.json` when available. Decoder uses explicit `CodingKeys` throughout: `convertFromSnakeCase` also rewrites data-bearing dictionary keys, so `counts["day_highlights"]` would silently become `"dayHighlights"`. The CI tripwire for schema drift is a committed BLAKE2b of `trip_schema.json` asserted in the Swift suite — crude, but it fails in CI rather than in someone's hands, and the failure message says how to regenerate. |
| 2026-08-09 | claude | **I02 done → review.** Vendored pure-Swift RFC 7693 BLAKE2b (CryptoKit and swift-crypto have none). Parity is a committed file, `ios/Tests/StoryKitTests/Fixtures/expected_media_hashes.json`, asserted by the Swift suite *and* recomputed with `hashlib.blake2b` in `ios.yml` — a disagreement fails CI on whichever side drifted. First cut ran at 108 MB/s; removing per-block allocation and bounds checks in `compress` took it to ~600 MB/s, so a 400 MB clip hashes in under a second. |
| 2026-08-09 | claude | **I01 done → review.** Swift 6 package, four targets + three test targets, iOS 17 + macOS 14 so `swift test` runs hostless on CI. `ios.yml` runs on `macos-15`, path-filtered to `ios/**`; the existing Python jobs are untouched. "In CI" half of the criterion pends the first push. |
| 2026-08-09 | claude | **Tracker created.** Eight decisions recorded (D1–D8). Two changed task scope from the design docs: `ContentHash` moves from M1 to Wave 0, because the export ledger's key is the hash of the *exported* bytes and only the phone at export time knows which `PHAsset` produced it; and the reel (Wave 3) outranks curation (Wave 4), which puts the music picker on the critical path. Also found while checking references: `ios_client_app.md:225` cited the faststart fix as `cf2c75e`, which is not reachable from `HEAD` — the commit was amended, the live one is `9eb53ab`, and the old object will be garbage-collected. Fixed. A commit hash in a doc is a reference that can rot silently, since nothing resolves it. |
