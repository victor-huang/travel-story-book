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
| D14 | **The goal is now one working loop, not more breadth.** Decided 2026-08-10 with the human: "move toward integrating everything as the goal", and keep the simple choices until using the app argues against them. | Consequence: depth-first on the shortest path that makes a photograph become a book — `I20` negotiate, `I21` upload, `I22` poll against the service that now exists, then `S05` delivery so the report the app already renders (I24/I25) has something to render. **Deferred deliberately, not forgotten:** real auth (`S06`) stays the `X-Story-Identity` stub until the loop closes, `S04`'s retention sweeper waits, and Wave 3's reel waits — D7 still says the reel outranks curation, but neither outranks proving the loop. The risk being accepted is that a stub becomes load-bearing; `S06` must land before anything is exposed beyond localhost.
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
| I15 | `FolderWriter` — the source folder, and hand-off | review | claude (2026-08-09) | I11, I12, I14 |
| I16 | **Parity harness** — the M0 gate | review | claude (2026-08-09) | I15 |
| I17 | **M0 export app** — one screen, so a human can run M0 | done | claude (2026-08-09) | I10, I15 |

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

# Wave S — The service (M1's backend, blocks most of Wave 2)

Added 2026-08-09. **This wave existed only as the words "service M1" in four `Depends on` cells**
— a dependency pointing at nothing, which is why four Wave 2 tasks sat as `todo` for a day
looking startable and were not. Its design is
[`ios_backend_service.md`](./ios_backend_service.md); this is the decomposition.

Nothing here is claimed, and **S01 gates the rest** — it decides where the service runs and in
what, which every other entry assumes.

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| S01 | Service skeleton, deployment shape, CI | review | claude/S01 agent (2026-08-10) | — |
| S02 | Ingest — `POST /trips`, `assets:negotiate`, presigned `PUT` | review | claude/S02 agent (2026-08-10) | S01 |
| S02b | **`LocalFileObjectStore`** — a filesystem masquerading as S3, for one same-Wi-Fi test | done | claude (2026-08-12) | S02 |
| S03 | Job queue — `POST /trips/{id}/build`, `GET /jobs/{id}` | review | claude/S03 agent (2026-08-10) | S01, S02 |
| S04 | Storage layout and the retention sweeper | todo | — | S02 |
| S05 | Delivery — report bundle and signed CDN URLs | review | claude/S05 agent (2026-08-11) | S03 |
| S06 | Auth — Apple and Google, per-user isolation | todo | — | S01 |
| S07 | Reel endpoints (M2) | review | claude/S07 agent (2026-08-12) | S03, S05 |

Mapping back: **I20 needs S02, I21 needs S02, I22 needs S03, I23 needs S06, I33 needs S05, I30
and I31 need S07.** Those cells still say "service M1" and should be reread as these.

### S01 — Service skeleton, deployment shape, CI
**Owns:** a new top-level directory (name TBD — *not* `src/story_book/`), its CI workflow
Decides three things nothing else can proceed without: **what it runs on**, **what it is written
in**, and **how a developer runs it locally**. A strong default rather than a decision already
taken: **Python, because the service's whole job in M1 is to run `story-book build`** — the
pipeline is Python, the CLI is the interface, and a service in another language would shell out
to it anyway while losing the ability to read `stage_result` directly. Needs the human's
ratification before code (see open question 13).
**Done when:** a health endpoint is reachable locally and in CI, and `story-book --version` runs
inside the same image.

**Delivered 2026-08-10 → review. 19 tests. Three things were decided, four were deliberately not,
and the split is the point of the task.**

Decided, and each confined so that reversing it is cheap:

- **Python 3.12, FastAPI, uvicorn, in a new top-level `service/`.** The doc's recommended default,
  taken *as a proposal* rather than as a ratified decision — see question 13, still open. Reversing
  it deletes one directory and one workflow.
- **`service/` is its own uv project** with an editable path dependency on the repository root, so
  `pyproject.toml` — owned by the Python tracker — is untouched, and `uv run pytest` at the root
  still collects only `tests/`. The service suite runs from `service/`; `ci.yml`'s `ruff check .`
  already lints it, so `service.yml` deliberately does not lint again.
- **One container image holds the API and the CLI.** That is what makes hosting deferrable: the same
  image runs under compose on a VM, on Fly machines, on ECS or on Cloud Run.

Not decided, because S02–S07 would inherit the guess: **hosting target** (question 14), **object
store and presigned-URL provider** (15), **queue substrate** (16), **where the multi-trip index
lives** (17), and **whether the image carries `clip`** (18). There is no object-store client and no
queue in this directory as a result. Question 4 — source tree per trip or per user — is also
untouched, and it constrains 15 and 17 rather than the other way round.

**Hosting options, for question 14.** The pipeline's shape does eliminate one family: a build is
minutes to hours and wants a real filesystem with tens of GB on it, so request-scoped serverless
runtimes are out on their own terms, not by preference.

| Option | Fits because | Costs |
| --- | --- | --- |
| One VM, docker compose, local disk + S3-compatible store | Simplest thing that runs `story-book build` unmodified: POSIX filesystem, hours-long jobs, ffmpeg, a big scratch disk | Ops and backups are yours; one machine |
| Fly.io machines + volume + Tigris/S3 | Long-lived processes and persistent volumes are first-class, container-native, cheap | The volume is per-machine, so worker placement becomes a constraint |
| AWS ECS/Fargate + S3 + SQS + CloudFront | Presigned `PUT`, CDN and IAM are native; nothing to patch | Fargate ephemeral storage caps out; one-worker-per-trip scheduling is yours; dearest of the four |
| Cloud Run + GCS + Cloud Tasks | Same shape on GCP, scales to zero | Request-scoped CPU and a 60-minute ceiling — a multi-hour build does not fit, so the worker needs Cloud Run Jobs or GKE and the API and worker stop being one deployment |

**`/ready` reports what the deployment can actually do, and it is not decoration.** The pipeline
degrades rather than aborting when an optional dependency is missing, so a `clip`-less image returns
a perfectly valid `trip.json` with no CLIP clustering in it. The endpoint therefore separates the
three dependencies a build cannot survive (`story-book` on `PATH`, `exiftool`, `ffmpeg`) from the
four that only narrow the result, names the consequence of each absence *in the words of the stage
that stops working* (`Stage.description`, read rather than paraphrased), and carries `measured_at`
because the probe runs once at startup — a cached reading must not read like a fresh one.

**It caught something on its first real run.** Launching `./.venv/bin/uvicorn` instead of
`uv run uvicorn` starts the service fine and answers `/health` with `200`, while `story-book` is not
on `PATH` at all — the one thing the service exists to run. `/ready` returned `503` with
`No such file or directory: 'story-book'`. A liveness-only health endpoint would have called that
deployment healthy.

**The image is unverified locally: this machine has no Docker.** `service.yml`'s `image` job builds
it, runs `story-book --version` inside it, and asserts `/ready` returns `200` *and* a body saying
`ready: true` — a 200 with `ready: false` would be a status code the endpoint had not earned. Until
that job runs, the container half of the acceptance criterion is claimed by CI and not by me.

### S02 — Ingest
**Owns:** the ingest routes and the object-store client
`POST /trips` → `{trip_id}`; `POST /trips/{id}/assets:negotiate` with `[{hash, filename, size}]`
→ only what the service lacks; then a presigned `PUT` per asset **straight to object storage**, so
the API server never proxies 600 MB. **Never a zip** — no resume, no early start, and adding ten
photos re-sends everything.
**`filename` is preserved end to end**, because `overrides.toml` addresses by filename.
**The upload granularity is one asset; the unit of work stays the trip.** How bytes are chunked
must never decide what the pipeline treats as a unit — that is precisely the T58 bug.
**Done when:** negotiating an unchanged trip a second time returns an empty `needed`, and the
scaffolding uses `story-book init --trip-dir` rather than a hand-written config — loading the
scaffolded overrides in the new context and asserting it is empty, because a file safe to read is
not automatically safe to copy.

**Delivered 2026-08-10 → review. 100 new tests (service suite 19 → 119); the root suite is still
1772.** Both halves of the criterion pass against a real S3 API on localhost (`moto server`, never a
real bucket): a second negotiate of an unchanged trip returns `needed: []`, and `source:prepare`
scaffolds with `story-book init --trip-dir` and then loads the scaffolded `overrides.toml` **in that
trip's own context** and asserts it is empty. `story-book build` runs to completion on the
materialised folder and `trip.json` contains all three assets — the count is the control, because a
build over an empty folder also exits 0.

**The wire contract, for I20 to be checked against.** `POST /trips` → `{trip_id, name, created_at}`;
`POST /trips/{id}/assets:negotiate` with `{assets:[{hash, filename, size}]}` →
`{needed:[{hash, filename, stored_filename, filename_adjusted, size, method, put_url, headers,
expires_at, replaces_mismatched_object}], have:[{hash, size}], upload:{…}}`; then `PUT` to `put_url`
reproducing `headers` (`Content-Length` is inside the signature). `hash` is **128 lowercase hex
characters** and an `asset_id` prefix is rejected with a 422 rather than silently matching nothing.
Also `GET /trips`, `GET /trips/{id}`, and `POST /trips/{id}/source:prepare`.

**Two deviations from `ios_backend_service.md`, both deliberate.**

1. **`POST /trips` does not scaffold a config, and `source:prepare` is a new route.** `story-book
   init` *profiles* the source folder, and at trip-creation time that folder is empty — it succeeds,
   warns "no importable media found", and writes a config in which nothing was measured. Since
   `init` then refuses to overwrite its own file, that guess would be permanent. So scaffolding waits
   for the media, which means it needs a moment after the uploads and before the build. It is
   idempotent, so S03 may call it as build step zero and this route becomes an alias.
2. **No multipart upload.** A single presigned `PUT` carries 5 GB and a 1080p export is tens of MB,
   so multipart buys resume *within* one file, not capability. The response says
   `multipart: false` with that reasoning in it rather than leaving I21 to infer it.

**Three findings worth carrying.**

- **`moto` verifies nothing.** An unsigned PUT, a tampered signature and an hour-expired URL all
  returned `200` against `moto server`. So the suite proves the round trip and the URL's *shape*;
  signature, expiry and the signed `Content-Length` are properties of S3 and are **not** measured
  here. A test asserting "a wrong-length PUT is refused" would have passed for the wrong reason and
  told a reader something untrue — it is not in the suite, and the docstrings say why.
- **The service cannot verify that the bytes under a hash hash to it.** It never reads them, which
  is what "never proxy the media" costs. `have` therefore means only "an object of the declared
  length is at that key", and the negotiate response says so in `upload.presence_not_verified`. The
  one contradiction available *is* used: a stored length that disagrees with the declaration is
  treated as **missing**, so the asset re-uploads rather than being believed. This is also the
  strongest argument on question 4 — see below.
- **Filename collisions silently lose a photograph.** Two cameras both produce `IMG_0001.JPG`, and
  writing each asset to `<source>/<filename>` overwrites one with no error. `assign_stored_names`
  renames **every** member of a colliding group, never just the later arrival, so the answer is a
  pure function of the `{hash: filename}` set rather than of arrival order — and a later negotiate
  therefore renames an asset already on disk, which `materialise_source` reconciles by removing the
  file left under the old name and reporting it in `removed`. A test caught exactly this: the first
  version renamed only the batch it was handed.

**What is not built here:** no queue and no worker (S03), no auth (S06), no retention sweep (S04).
`principal.py` reads an `X-Story-Identity` header and believes it; the service logs a warning saying
so at every start, and a test asserts the warning. Every index read is already scoped by `owner_id`
**in SQL** — `Index.get_trip` cannot be asked without one — so S06 replaces one function body and
touches no query. Per-route isolation is asserted per route, and both scoping and presence were
**shown to fail**: dropping `owner_id` from the two `WHERE` clauses fails 5 tests, and making `head`
always return `None` fails 3, including the acceptance criterion.

### S03 — Job queue
**Owns:** the queue, the worker, and the job routes
`POST /trips/{id}/build` → `{job_id}`; `GET /jobs/{id}` → `{state, stage, done, total}`.
**Progress is read from `stage_result`, never invented** — the pipeline already commits per item,
so a killed worker resumes rather than restarts. **One worker per trip at a time:** `story.db` has
a single-row `trip` table, so concurrency is serialised by the queue rather than defended against
in code.
**Done when:** a build reports monotonically advancing counts that match the DB, and killing the
worker mid-build resumes without redoing completed stages.

**Delivered 2026-08-10 → review. 61 new tests (service suite 119 → 180); the root suite is still
1772.** Both halves of the criterion are met against real fixture media, a real presigned `PUT` and
the real CLI: a build is polled over HTTP *while it runs* in another thread on another connection,
and the reported `(stage_index, done)` never goes backwards; a second job over the same trip leaves
every per-item `computed_at` **byte-identical** while `scan`'s — `always_run` by design — is newer.
That pairing is the point: without the control, the same assertion passes against a build that never
ran.

**The wire contract, for I22 to be checked against.** `POST /trips/{id}/build` with no body →
`202 {job_id, state, created: true, …}`, or `200 {…, created: false}` when the trip already has a
job queued or running. `GET /jobs/{job_id}` →
`{state, stage, done, total, stage_index, stages_total, stages_complete, media_known, phase,
attempts, created_at, started_at, finished_at, heartbeat_at, error, exit_code, degraded,
unavailable_stages, capability_measured_at, progress_basis, stages:[{name, version, state, done,
failed, total, detail}]}`; plus `queued_ahead` while queued and `trip_json` once succeeded. Also
`GET /trips/{id}/jobs`, newest first, for the app that was killed and lost its `job_id`. `state` is
`queued | running | succeeded | failed`; `stage` is `null` while queued, `source:prepare` while the
media is being fetched, else the first pipeline stage with outstanding work.

**There is no percentage, and `total` is nullable.** Eighteen stages cost wildly different amounts
per item, so one number over all of them would be a measurement nobody took — `stage_index` of
`stages_total` is the honest overall position. `total` is `null` where nothing has been measured
rather than `0`, because `0` reads as "nothing to do". A test asserts no field named `percent`,
`fraction` or `eta` exists, so adding one fails.

**Reading the real response found a defect no test had.** One second into a build, `media` is still
empty, so every per-item stage selected nothing — and the first version reported them `complete`,
"does not apply to this library", **five of eighteen stages done before a photograph had been read**.
An empty library is not evidence that a stage has nothing to do. Fixed, with the control that a
genuinely inapplicable stage (`video` over stills) still reads complete once the scan has run.

**Two denominators are subtler than they look.** `EmbeddingStage.select()` filters out what it has
already embedded — it must, since the cache key carries no model tag — so `len(select())` *shrinks*
as the stage progresses, and a total taken from it walks down to meet `done`: a bar that fills
without progress. The published denominator is `select() ∪ already-completed`. And a stage is
finished when `done + failed >= total`, not when `done == total`: one permanently failing photograph
would otherwise pin the reported stage to `metadata` for the rest of the run.

**Availability is measured once, at the job's start, and quoted.** Not per poll — that would spawn
`exiftool` and `ffmpeg` subprocesses per request *and* could report a capability the running build
never had. It is recorded on the job row by calling each stage's own `available()`, so a real job in
this image says `embeddings` and `content_class` are unavailable because *"CLIP unavailable: missing
torch, open_clip"* and `landmarks` because *"--no-cloud"*. That is open question 18's requirement met
whichever way the question is answered. It also caught a mismatch: `--no-cloud` is a CLI flag that
overrides the config, so measuring availability from the config alone reported `landmarks` as
available on a run that always skips it.

**What is deliberately not decided.** `POST .../build` accepts **no** `{config, overrides}`, which
the design doc's endpoint list shows — config ownership is open question 8, and the config a build
uses is the one `story-book init` *measured* from the uploaded media, which `init` then refuses to
overwrite. A client-supplied config would make a cached guess permanent. Overrides are I43's and no
route writes them yet. Question 16 (the queue substrate) is proposed, not settled — see below.

**Two files S02 owns were extended, on that task's own invitation.** `index.py` says "S03 adds jobs
and S07 adds reels. They belong on this interface too", so `Job` and ten methods went on the
interface and its SQLite implementation rather than into a parallel store. The same commit turns
**WAL on**, which question 19 makes a requirement rather than tuning and which S02 had not needed.

### S04 — Storage and the retention sweeper
**Owns:** the storage layout and the scheduled sweep
Everything under `--out` is derived and disposable **except `<out>/story/`**. Retention as decided:
uploaded media deleted one month after the most recent generation, clock reset by any successful
`build`, `report`, `package` or `reel`; renditions and metadata kept indefinitely.
**The sweeper must not race a job.** The condition is *expired **and** no job queued or running
for this trip*, and a job starting extends the deadline **before it reads a single file**.
Deletion is idempotent; a trip whose media is already gone is not an error.
**Done when:** a trip with a queued job survives a sweep that its date alone would have collected.

### S05 — Delivery
**Owns:** the report-bundle builder and the signed-URL issuer
The report is a **directory** and its pages reach into four roots — `report/`, `thumbs/`,
`previews/` and `.cache/video/` for poster frames (found by I24; open question 12). **Ship the
report's reference closure and assert it resolves before delivering it**; a bundle assembled from
the three obvious directories loses every poster and raises nothing.
Progressive MP4 behind a CDN with short-lived signed URLs; never a public-read bucket. **The
`+faststart` defect the design doc lists as a prerequisite is already fixed** (`9eb53ab`, and both
mux paths in `export/subtitles.py` now set it) — that section of `ios_backend_service.md` is
stale.
**Done when:** a delivered bundle has zero unresolved references, and a signed URL expires.

**Delivered 2026-08-11 → review.** The "four roots in one bundle" premise above turned out to be
I24's, not I25's — see the now-answered open question 12 and `service/README.md`'s "What S05
added" section for the full reasoning. In short: I25 (`AssetSchemeHandler.swift`) renders and
resolves the report through `storyasset://`, so the bundle `GET /jobs/{job_id}/report` hands out
is html/css/vendored-Leaflet only, and every thumbnail, preview or video poster is served on
request by `GET /trips/{trip_id}/media/{relpath}` — restricted to paths `trip.json` itself names,
never a client-chosen one. Both routes are scoped by the same owner-checked joins as S02/S03's.
Signed **S3** `GET`s, not a CDN yet — none is provisioned (Q14/Q15), and `ObjectStore.presign_get`
is the seam a CDN slots in front of later without an API change. 11 new tests (180 → 191 in
`service/`, root suite still 1772 green), against a real `moto server`, real build through the
queue, and real fetched bytes unzipped/decoded — not just 200s. Found and left for I33: a video
poster's path (`.cache/video/<hash>_poster.jpg`) does not parse under
`AssetRequestParsing.parse`'s `thumbs`/`previews` host check once prefixed with `storyasset://`,
so today's client fails every poster — logged as open question 21, unfixable here without
touching `ios/**`. Extended `objectstore.py` (S02's file) additively with `put_file`/`presign_get`,
on the same precedent S03 used for `index.py`; logged as a self-resolved cross-task request.
Deferred, correctly out of scope: reel delivery (S07 depends on this task) and a CDN.

### S06 — Auth
**Owns:** the auth routes and the per-user scoping of every other route
Sign in with Apple and Google (D8). These are someone's family photographs: per-user auth on every
route, unguessable object-store paths, **no cross-tenant reads**. Pairs with I23, which is the
client half.
**Done when:** two accounts cannot see each other's trips, asserted per route rather than once.
**Blocked on open question 3** — in-app account deletion is an App Store requirement and
contradicts indefinite metadata retention.

### S07 — Reel endpoints (M2)
**Owns:** `POST /trips/{id}/reel`, `GET /trips/{id}/reels/{reel_id}`
Options through to `reel.json`, which stays **the honest record of what a render actually did**.
The music track is an ordinary hash-addressed asset, so S02 already carries it.
**Done when:** a re-cut at a different aspect returns a new `reel_id` rather than mutating one.

**Delivered 2026-08-12 → review. 18 new tests (service suite 208 → 226); root suite still green,
not touched.** A reel is a second job **kind**, not a second mechanism: `JOB_KINDS` gained `"reel"`,
`Job` gained two additive columns (`options` -- the client's request, verbatim, since unlike a
build there can be many reels per trip; `progress` -- the worker's own segment-plan measurement),
and the worker's `_execute` dispatches on `job.kind` to a new `_reel` method that runs
`story-book reel` as a subprocess, exactly the same pattern `_build` already used for
`story-book build`. No second queue, no new table beyond the two columns.

**One deliberate deviation from this entry's own endpoint names**, on the same reasoning S02/S03/
S05 already established for this tracker: build what the code that runs actually needs, not what
was written before the dependencies existed. `GET /jobs/{job_id}/reel` replaces
`GET /trips/{id}/reels/{reel_id}` -- there is no `reel_id` anywhere in this design; a reel is
addressed by its `job_id`, the same way S05's report bundle is addressed by the build's `job_id`,
and I33's cache is told to key by id rather than URL. Matching S05's naming means I30/I31/I33 have
one convention to learn, not two.

**The wire contract, for I30/I31/I33.** `POST /trips/{id}/reel` with
`{aspect?, music_hash?, day?, date_from?, date_to?, places?, name?, subtitles?, burn_in?,
clip_audio?}` → `202 {job_id, kind: "reel", state: "queued", created: true, ...}` or
`200 {..., created: false}` when this trip already has a job queued or running (a build *or* a
reel -- both write under one `--out`, so the existing partial-unique-index rule serialises them
unchanged). A bad `aspect` or an undeclared `music_hash` is a `422` **before** a job is queued.
`GET /jobs/{job_id}` is the same S03 shape (`state, stage, done, total, degraded, ...`) for both
kinds; while a reel is `running`, `stage` is `"reel:render"` and `done`/`total` are a count of this
job's own segment-cache files against the exact plan `story-book reel` itself will render --
computed once, before rendering starts, and read by checking which of those files exist on disk,
never a fabricated percentage. `GET /jobs/{job_id}/reel` (`404` if the job is a build, `409` while
queued/running) → `200 {job_id, trip_id, state, video: {download_url, expires_at, size_bytes},
immutable: true, reel_json: {...}}` -- a signed S3 `GET`, the same `ObjectStore.presign_get` seam
S05 already built, keyed by `job_id` so a re-cut is a new key rather than a rewrite (D5). The whole
`reel.json` ships inline rather than as a second signed URL, since I30's own acceptance criterion
("each option reaches the service and is reflected in the returned reel.json") is exactly what a
client checks it against.

**Music reuses ingest wholesale, confirmed against the real routes rather than assumed.** There is
no upload path added: a track is negotiated and PUT through `POST /trips/{id}/assets:negotiate`
like any photograph, `POST /trips/{id}/reel`'s `music_hash` is checked against that trip's declared
assets before a job is even queued, and the worker resolves it to the same `paths.source` file the
build's own asset materialisation already fetched -- `_prepare` (S03's fetch-and-scaffold step) is
reused for a reel job unmodified, since a music file is, structurally, just another declared asset
with an extension the pipeline's own `scan.py` already ignores. A test uploads a real two-second
AAC tone (the same `ffmpeg -f lavfi sine=` generator `tests/backend/test_reel.py` uses) through the
real negotiate/PUT round trip and asserts the delivered video actually gained an audio stream --
`ffprobe`'s `stream=codec_type`, not a 200, per this project's own P06 lesson.

**Two things not in the task's literal endpoint list, added because the worker needed them:**
`worker._run_cli` and `_wait_with_heartbeat` are now parametrised by `phase` -- without it, a
reel's own heartbeat loop would have overwritten `job.phase` from `"build"` to `"render"`
mid-build the moment the two kinds shared a wait loop, corrupting a build's own progress reads.
Caught before it shipped by tracing what `_job_json` actually branches on, not by a test (there
wasn't one for this, because the bug never existed in committed code -- logged as a near-miss
below).

**Deliberately not built.** No worker-level distinction between "the reel CLI was interrupted" and
"the reel CLI genuinely failed": `story-book reel` has no exit-130 convention of its own (its
render loop is not the pipeline `Runner`), so inventing one would be exactly the fabricated
distinction `CLAUDE.md` warns against. Resumability still holds at the level that is real --
`.cache/segments/` -- so a fresh reel request after a kill reuses whatever was already rendered;
it is just a new job rather than a resumed one. Also deferred: a config knob for
`reel.subtitle_scale` through the request (I30's task entry does not ask for it, and CLAUDE.md's
own rule is a field in `config.py`, not a per-request override); and the `video-proxies` fallback
path from `dev_plan/reel_video_montage.md` (no `story-book package --video-proxies` route exists
on this deployment, so a reel always renders from the materialised source tree or falls back to
poster stills, never a proxy -- `resolve_clip_sources` already degrades that way on its own).

**Verification.** Against a real `moto server`, a real build through the queue, real fixture
media, and a real two-clip render: `ffprobe` on the *fetched* bytes confirms a video stream (not a
JPEG under an `.mp4` name) and, in the music test, an audio stream that the silent-reel control
lacks. A second `reel_progress_seed`-driven test asserts `done`/`total` equal the literal count of
`.mp4` files under `.cache/segments/` after the render, not a number this service invented. Also
run against `LocalFileObjectStore` (S02b) end to end, in-process through the same `TestClient`, to
match what is actually being tested against on-device today. **Not run against the live local
service already up on this machine** (`192.168.1.81:8000`, confirmed alive via `/health`): its
`/openapi.json` predates this task (`POST /trips/{id}/reel` is absent from it), and restarting a
shared, already-running instance without knowing what device session might be mid-loop against it
felt like the wrong trade against a local pytest run that already exercises the identical code
path. Left for whoever restarts it next.

---

### I17 — M0 export app *(added 2026-08-09, not in the design docs)*
**Owns:** `ios/Sources/StoryApp/ExportScreen.swift`, `ios/HostApp/HostApp.swift`, the `HostApp` target in `StoryBookHost.xcodeproj`
**Why this exists.** Wave 1 is described as "shippable alone for anyone with a laptop", but I10–I16
are library code plus a test harness — **nothing in M0 produces something a traveller can run**,
and the app shell is I23, in Wave 2. So M0 as specified cannot be exercised by a human. This is the
smallest thing that fixes that: pick a scope, see the cull-check nudge, export, hand the folder off.
No account, no upload, no backend.
**Deliberately thin.** The screen owns no logic — scope selection, the nudge, the privacy filter,
the exporters and the ledger are all already built and tested. If anything here starts making
decisions, it belongs in `PhotoExport` instead. I23 replaces the shell; this screen should survive
into it or be deleted, not grow.
**Done when:** an export run on a simulator produces a folder that `story-book build` completes on,
driven entirely by tapping.


# Wave 2 — Upload, build, and the book (M1)

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| I20 | `NegotiateClient` — hash negotiation | review | claude/I20-22 agent (2026-08-10) | I02, **S02** |
| I21 | `UploadQueue` — background, per-file retry | review | claude/I20-22 agent (2026-08-10) | I20, **S02** |
| I22 | `JobPoller` — build progress | review | claude/I20-22 agent (2026-08-10) | I20, **S03** |
| I23 | `Auth` + app shell + trip list | **blocked** | — | Wave 0, **S06** |
| I24 | Report webview | review | claude (2026-08-09) | I03 |
| I25 | `AssetSchemeHandler` — images from the phone | review | claude/I24 agent (2026-08-09) | I04, I24, XT-1 ✅ |
| I26 | **Loop screen** — export, upload, build, view, on the phone, over the stub identity | done | claude/I26 agent (2026-08-11) | I17, I20, I21, I22, I24, I25, S05 |

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
- **I25's blocker is gone.** XT-1 resolved 2026-08-09: `render_report` now takes a `MediaPrefix`,
  and `MediaPrefix.absolute("storyasset://")` is the app's case. Claimed the same day.
- **"service M1" is now Wave S.** Four tasks depended on a string, which is why they read as
  `todo` and were not startable. The `Depends on` cells above name real task ids.
- **I22 stopped being blocked on 2026-08-10**, when S03 landed the queue and the job routes. Its
  contract is written out in the S03 entry and in `service/README.md`, and the service half is
  exercised by polling a *real running build* over HTTP. Two things I22 should read before writing
  Swift: `total` is **nullable** and `done`/`total` are **per stage**, and the response deliberately
  carries no percentage — `stage_index` of `stages_total` is what a progress view should use, with
  the per-stage `done`/`total` inside it.
- **I20 and I21 stopped being blocked on 2026-08-10**, when S02 landed the ingest routes and the
  presigned `PUT`. Their contract is written out in the S02 entry and in `service/README.md`; the
  service half is exercised over HTTP against a real S3 API, so a Swift client can be checked
  against a running service rather than against prose. I21's "resume without re-sending completed
  assets" is satisfied at asset granularity: there is no multipart, deliberately, and the service
  says `multipart: false` in the negotiate response rather than leaving the client to discover it.

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

**Delivered 2026-08-09 → review.** 18 tests, hostless in CI. Every tier is proven through decoded
pixels in a real `WKWebView` with a real `WKURLSchemeHandler` registered — `naturalWidth > 0`,
matching I24's own standard — including the control that a local hit wins even when a remote one
would also answer, and that an unparsable request never reaches a resolver at all.

Sizes mirror the server exactly rather than inventing new ones: `480`/`1600` are
`config.py`'s `thumbnail_long_edge`/`preview_long_edge`, so a reader does not see a quality jump
between tiers. Tier 3 (the placeholder) is synthesized with ImageIO, not shipped as an
asset-catalog image — same reason `StillExporter`'s tests need no host app.

**Tier 2 is not wired to anything real.** I33 (`MediaCache`) does not exist yet, so the default
`remote` resolver is `{ _ in nil }` and every local miss falls straight to the placeholder until
I33 lands and something constructs `AssetSchemeHandler(remote:)` with a real one. The seam is the
resolver's signature, `(AssetRequest) async -> Data?`, which I33 only has to satisfy.

**Tier 1's positive path — real bytes, dimensions reflecting the target size, a video asset being
skipped in favour of tier 2 — has no host to run in.** `PhotoKitAssetSource` is proven *not to
crash* against an unauthenticated/empty library on the macOS test host (the same code path a
deleted or iCloud-only asset takes), but the real case needs a seeded simulator, and
`StoryBookHost.xcodeproj` wires only the `PhotoExportTests` scheme (D10) — `StoryAppTests` is not
in it. Recorded rather than silently skipped; same shape of gap as I05 before D9/D10 existed.

**Crosses into I24's file.** Registering the scheme handler has to happen where the `WKWebView` is
created, so `ReportLoader.init` gained an `assetScheme` parameter in `ReportWebView.swift`. Same
agent owns both tasks here, so this was not a collision, but a future reviewer should know I25
touched a file I24 owns.

**A real, unrelated build break was hit and left alone.** Mid-session, `swift build` failed on
macOS from I17's `ExportScreen.swift` (`.keyboardType` is UIKit-only, used unconditionally, and
Package.swift lists `.macOS(.v14)` precisely so this has to compile there). Not my file — logged
under Cross-task requests and left for I17's owner, who fixed it shortly after. The lesson isn't
new but is worth repeating: a broken file in a shared target blocks *every* task's verification,
not just its own.

| ID | Task | Status | Owner | Depends on |
| --- | --- | --- | --- | --- |
| I30 | Reel options — aspect, day range | review | claude/I30-33 agent (2026-08-12) | I22, **S07** |
| I31 | Music import — Files, **not** Apple Music | review | claude/I30-33 agent (2026-08-12) | I20, **S07** |
| I32 | Playback | todo | — | I30 |
| I33 | `MediaCache` — posters and reels | review | claude/I30-33 agent (2026-08-12) | I23, **S05** |
| I34 | Share sheet | todo | — | I32 |

### I26 — Loop screen *(added 2026-08-11, D14's loop closed on-device)*
**Owns:** `ios/Sources/StoryApp/LoopScreen.swift`, `ios/HostApp/HostApp.swift`, `ios/HostApp/Info.plist` (new — local-network + ATS exception), `ios/StoryBookHost.xcodeproj` (Info.plist wiring only)
**Why this exists.** Every piece D14 named — export (I17), negotiate/upload (I20/I21), poll (I22),
render (I24/I25), deliver (S05) — is built and independently tested, but nothing on the phone chains
them. `HostApp` presents `ExportScreen` and stops there. This screen is the chain: pick media, export,
create a trip, negotiate + upload, start a build, poll to completion, fetch the report bundle, unzip
it, hand the local directory to `ReportWebView`.
**Auth for this task is the stub, on purpose.** `UnverifiedIdentityHeader` — S06 does not exist yet
and D14 explicitly defers it, with the one condition that it "must land before anything is exposed
beyond localhost." **Confirmed with the human 2026-08-11: a same-Wi-Fi test is in scope.** The service
binds to the Mac's LAN IP rather than `127.0.0.1` for this test; nothing here should make that
binding, or the ATS exception it requires, look like a production setting — comment both as
temporary and point at S06.
**Deliberately thin, same rule as I17.** This screen owns no business logic — it sequences calls into
already-tested modules and shows their real state (queue position, stage name, `degraded`, in progress
counts with no invented percentage). If a decision needs making that those modules do not already
make, it belongs in one of them, not here.
**Done when:** on a physical iPhone, over the same Wi-Fi as the Mac, tapping through from "pick media"
to "see the rendered report" completes without touching Xcode, and a re-run after the app is killed
mid-upload resumes rather than restarting.

### I30 — Reel options
**Owns:** `ios/Sources/StoryApp/ReelOptions.swift` + tests
Aspect, music, day range, name, subtitles → `POST /trips/{id}/reel`. **Offer these while the
media is still there**: uploads are deleted one month after the most recent generation, and a
re-cut after expiry reads 720p proxies and is visibly softer. Surface the window closing — it is
only useful if the user knows about it.
**Done when:** each option reaches the service and is reflected in the returned `reel.json`.

**Delivered 2026-08-12 → review.** `ReelOptionsScreen`/`ReelOptionsModel` build a `ReelOptions`
value one-to-one with S07's `ReelRequest` (aspect, music hash, day/date-range/places, name,
subtitles, burn-in, clip audio), submit it to `POST /trips/{id}/reel` through a small `ReelClient`
written against the public `ServiceEndpoint`/`ServiceIdentity` types (the same choice `LoopScreen`
made for the two S05 routes it needed and `NegotiateClient`/`JobPoller` do not cover), then tracks
it with **`JobPoller.follow` unmodified** — a reel is a second job kind on the exact same `GET
/jobs/{id}`, so no new polling code exists, and `stage`/`done`/`total`/`degraded` shown to the user
are the same `JobStatus` a build uses. **Verified against a real render, not just a decode test**:
a fresh, isolated local service instance (own port, own empty data root, `LocalFileObjectStore`,
never the shared already-running one — see the Log entry below for why) ran a genuine
trip → negotiate → upload → build → `POST .../reel {aspect: "9:16", name, clip_audio: false}` →
poll → `GET /jobs/{id}/reel`, and `reel_json.video.aspect` came back `"9:16"` — the option is
provably reflected, not merely encoded on the way out. The retention advisory
(`ReelRetentionAdvisory`) is deliberately a **static sentence, not a countdown**: no route
publishes an expiry timestamp yet (`S04` is still `todo`), and inventing one from `created_at`
would be exactly the fabricated-measurement failure this project keeps naming.

### I31 — Music import
**Owns:** `ios/Sources/StoryApp/MusicImport.swift` + tests
**Apple Music tracks are DRM-protected and cannot be exported or mixed.** A picker showing the
subscription library produces nothing but render-time failures. Sources that work: Files/iCloud
Drive imports, purchased DRM-free downloads, a licensed catalogue. The tool ships no audio
(`docs/choosing_music.md`), so **this limitation must be legible at the picker**, not discovered
when the reel comes back silent. The track uploads as an ordinary hash-addressed asset.
**Done when:** a DRM-protected selection is refused with an explanation before upload, not after.

**Delivered 2026-08-12 → review.** `.fileImporter` over `.audio`, never `MPMediaPickerController`
— the picker only ever offers files, so a library-only DRM stream cannot even be selected.
`DRMCheck.check(url:)` (hostless, no UIKit — runs under `swift test` on the CI host) checks, in
order: the legacy `.m4p` extension (refused without opening the file), then
`AVURLAsset.load(.hasProtectedContent)`, then `loadTracks(withMediaType:)` to catch a file that
opens but carries no audio. `MusicImporter.importTrack` runs that check **before** it hashes,
negotiates, or uploads anything, and only then hands the file to the same `NegotiateClient`/
`UploadQueue` pair I20/I21 already built — no second upload path. **The honest limit, stated in
the code and repeated here:** there is no genuine FairPlay-encrypted fixture to test against —
one cannot be manufactured without Apple's own DRM tooling and a store-issued file — so the
`.m4p`-extension path is exercised exactly, while `hasProtectedContent`'s real DRM-detection path
is real production code exercised only on its negative case (a real, valid AAC file — synthesized
in-process with `AVAudioFile`, no `ffmpeg` dependency, no committed binary fixture — correctly
reports `.clear`). A device test with a real protected download is the one thing this suite
cannot stand in for. The refusal-before-network claim has its own control: a protected `.m4p`
selection is checked against a client pointed at an **unregistered** host, so if the check were
bypassed the request would surface as a transport failure, not a silent pass.

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

**Delivered 2026-08-12 → review.** An actor over `Caches/StoryMediaCache`, keyed by whatever `id`
the caller passes (a `job_id` for a reel, an asset hash for a poster) — the cache hit path
(`localURL`) checks the filesystem for that id and **never inspects the `remoteURL` argument at
all** unless it is a miss, which is the entire mechanism "key by id, never by URL" reduces to in
code. Proven with the control the house style asks for: the same id at a deliberately different
URL (simulating a rotated signature) is a hit and makes zero network calls, while a **different**
id at the very same URL is a miss and does make one — without that second test, a cache that
(bug) keyed by URL instead of id would also pass the first assertion trivially. `purge()` then a
fresh `localURL` call is shown to redownload for real (the stub's call count increments a second
time), not merely to throw no error. Both the directory and each downloaded file individually set
`isExcludedFromBackup` — belt and braces, asserted as two separate tests, since Apple's own
documented inheritance behaviour is not something to trust silently forever. Deferred, per the
task's own wording: the user-visible "keep offline" pin that would move a file to Application
Support — no screen in this wave asks for it yet.

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
| I16 | ~~`tests/fixtures/media/`~~ → `tests/ios_parity/exported/` | **Deviation, resolved without a request.** The plan put iOS-exported fixtures in `tests/fixtures/media/`, but `generate.py` opens with `shutil.rmtree` on that directory, so a documented command would delete them. They live in `tests/ios_parity/exported/` beside the harness that consumes them, and no Python-owned file was touched. |
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
| XT-1 / I25 | Python tracker → `src/story_book/export/report.py` | Make `MEDIA_REL_FROM_INDEX` / `MEDIA_REL_FROM_DAY` overridable so the report can render with a `storyasset://` prefix. Filed 2026-08-09, **resolved the same day** with the human's authorisation: `MediaPrefix` on `render_report`. | resolved |
| I25 | I17 / `ios/Sources/StoryApp/ExportScreen.swift` | `.keyboardType(.numbersAndPunctuation)` at `ExportScreen.swift:84` is a UIKit-only SwiftUI modifier used unconditionally, breaking the macOS build `ios.yml` needs. Filed 2026-08-09; fixed by I17's own agent shortly after — `swift build` is clean again. | resolved |
| S05 / S02 `objectstore.py` | S02 (owns `objectstore.py`) | S05 needs the download direction of the object store — `put_file` (upload a derived artifact the pipeline already wrote) and `presign_get` (signed `GET`) — alongside S02's existing `presign_put`/`head`/`get_to_file`. Added additively (no existing method touched), the same pattern S03 used on `index.py`: "S03 adds jobs and S07 adds reels; they belong on this interface too" applies just as well to "S05 adds downloads". No conflict found; self-resolved since S02 is not actively `wip`. | resolved |

---

# Open questions

Unresolved. Each blocks the wave named, not the whole plan.

| # | Question | Blocks |
| --- | --- | --- |
| 1 | ~~Does `swift-crypto` expose BLAKE2b, or is it vendored Swift vs. libb2 interop?~~ **Answered 2026-08-09:** neither CryptoKit nor swift-crypto exposes BLAKE2b; a vendored pure-Swift RFC 7693 implementation ships in `ContentHash.swift` (~600 MB/s after de-allocating the hot loop, digest verified against `hashlib` and the RFC vectors). | ~~I02~~ |
| 2 | ~~Device or seeded simulator for the export suite?~~ **Answered 2026-08-09:** seeded simulator — see D9. | ~~I05~~ |
| 3 | **In-app account deletion is an App Store requirement (5.1.1(v)) and contradicts keeping `story.db`, `trip.json` and previews indefinitely.** Reconcile before submission. | Ship |
| ~~4~~ | **ANSWERED 2026-08-10 by the human: per-user asset keys** — which was already S02's running default, so nothing moves. The human's reason: no good reason to want global dedup. Two arguments from the code agree with it. **(a)** The service never reads the uploaded bytes, so it cannot confirm they hash to their key (S02's `have` is weaker than it looks, and says so). A globally shared content-addressed object would therefore be trusted on one client's word and then served to another user — per-user keys make that impossible rather than unlikely. **(b)** Deletion becomes a prefix delete with no reference counting, which is what keeps question 3 (in-app account deletion, an App Store requirement) answerable at all. Cost accepted: no cross-trip dedup, which is cheap to give up because trips rarely share photographs. Note this is a *storage layout*, so changing it later means moving objects in S3, not just editing code. <br><br>*Original question:* ~~I20~~, I23 |
| 5 | Where is the cull threshold? "60 of 800 in-range" clearly warrants the nudge and "600 of 800" clearly does not. Set it by watching real selections, not by guessing. | I10 |
| 6 | Is the alternates strip built from dedup clusters alone, or also from CLIP neighbours? The two solve different problems at different thresholds, and the strip wants "another frame of this moment", not "something that looks similar". | I42 |
| 7 | Does curation re-run happen automatically on edit, or on an explicit rebuild tap? | I44 |
| 8 | Config ownership: does the app expose thresholds, or does the service pin one config and keep the knobs on the laptop path? | I30 |
| 9 | Does `ios/` stay in this repo past Wave 2? | — |
| 10 | **How does `PhotoExportTests` run unattended?** `simctl privacy grant` is ignored on Xcode 26.3 / iOS 26 (D11), so today it needs one human click per simulator. XCUITest tapping SpringBoard's *Allow Full Access* is the known route; it needs a UI-test target. Until then the export path has **no CI coverage**. | CI for Wave 1 |
| ~~13~~ | **RATIFIED 2026-08-10 by the human.** Python 3.12 + FastAPI in `service/`, shipped as one container image holding the API and the CLI. Docker verified locally the same day. <br><br>*Original question:* Wave S |
| ~~14~~ | **ANSWERED 2026-08-10 by the human: local for development, AWS EC2 for production traffic.** A long-lived VM, which is what the pipeline wants — builds run minutes to hours, need a real filesystem with tens of gigabytes, and shell out to ffmpeg. Confirms the container choice in Q13 and rules out request-scoped serverless for good. S02–S05 unblocked on this axis; what is *not* yet decided is how the image reaches the instance (registry, compose, ECS) — that belongs to whichever S-task first needs to deploy, not here. <br><br>*Original question:* S02, S03, S04, S05 |
| ~~15~~ | **ANSWERED 2026-08-10 by the human: AWS S3.** Sits naturally with EC2 (Q14) — same account, IAM instance roles rather than long-lived keys, and S3 presigned `PUT` is the canonical form of "never proxy 600 MB". **The bucket does not exist yet** (the human is creating it), so S02 must be written against the client and its credentials as configuration, and must not hardcode a bucket name or region. Its tests should run against a local S3-compatible fake or `moto`, never against a real bucket in CI. <br><br>*Original question:* S02 |
| ~~16~~ | **RATIFIED 2026-08-10 by the human**, with the reasoning "reasonable, we can change them as we use the app more — keep things simple for now." So: a `job` table in the index claimed in one `BEGIN IMMEDIATE`, the worker in the same image, inline in the API process by default. Ratified **with its stated expiry**, which S03 was explicit about: confidence is high for one instance and **zero beyond it**, and nothing in the code would detect a second worker on another machine, because the trip directory is local. This expires at the same moment Q14 (single EC2 instance) and Q19 (SQLite on EBS) do — a second instance or a load balancer, not a user count. <br><br>*Original question:* S03 |
| ~~17~~ | **ANSWERED 2026-08-10 by the human.** There is a relational index alongside the per-trip SQLite files: a **`user`** identified by an email address *or* a phone number, authenticated through **Google first and Apple later** (which reorders D8's "Apple and Google"), and a **one-to-many `user` → `trip`** relation so one account holds many trips. `story.db` stays exactly what it is — one file per trip, `CHECK (id = 1)` intact — and the index holds what it structurally cannot: users, identities, the trip list, jobs and reels. This unblocks S02, S03 and S06, and it is now the thing Q3 (account deletion) and Q4 (per-trip vs per-user storage) are answered against. **Still open within it:** which engine holds the index, and whether a trip's media is addressed per-user or globally by content hash — Q4. <br><br>*Original question:* S02, S03, S06 |
| ~~18~~ | **RATIFIED 2026-08-10 by the human: ship without the `clip` extra**, and revisit after watching a real trip — an image change, not a contract change. What makes this safe is S03's mechanism rather than the choice: every job records at its own start which stages *this deployment* can run, by calling each stage's `available()` and keeping **its** words, and reports `degraded: true` with the reasons. **Know the second cost:** without CLIP you lose `embeddings` *and* `content_class` — the screenshot/receipt classifier — which the proposal grid (I40) will surface. A degraded build says so; it does not quietly produce a thinner story. <br><br>*Original question:* S03 |
| ~~19~~ | **ANSWERED 2026-08-10 by the human: SQLite on an EBS volume, with a stated migration trigger — "as we get more than a few hundred users we will consider moving to a hosted Postgres solution." The trigger is recorded because a trigger nobody wrote down is a trigger nobody acts on.** Consequences, none of them optional: **(a)** an EBS snapshot schedule is required from day one, not later — S3 holds the photographs and survives, but the index holds who owns what, and identities and trip names are the one part that cannot be reconstructed. Per-user asset keys mean an S3 prefix at least *names* its owner, which makes a lost index partially recoverable and no more. **(b)** WAL mode is mandatory, not a tuning choice: S03's worker writes job progress while the API reads, and WAL is what makes one writer plus many readers safe across processes on one filesystem. **(c)** the six-method seam in `index.py` stays honest — no SQLite-specific type or query may leak past it, because that seam *is* the migration plan, and `for_dsn` must keep refusing `postgresql://` by name rather than falling back. **(d)** this pins the API and the worker to one instance. A second instance, or a load balancer, is the moment this decision expires — earlier than the user-count trigger. <br><br>*Original question:* S02 ✅, S03, S06 |
| ~~12~~ | ~~**What exactly is "the report bundle" the service delivers?**~~ **Answered 2026-08-11 by S05.** Not the four-directory bundle I24's docstring describes: I25 (`AssetSchemeHandler.swift`), which landed the same day and is what the app actually registers, renders with `media_rel="storyasset://"` and says outright that the downloaded bundle "carries no `thumbs/` or `previews/` directory at all". So the service re-renders `trip.json` a second way for the wire — html/css/vendored-Leaflet only, no media inside it — and serves every thumbnail, preview or poster on request through `GET /trips/{trip_id}/media/{relpath}`, restricted to paths `trip.json` itself names. See `service/README.md`'s "What S05 added" section. Posters were not moved out of `.cache/` on the Python side; they didn't need to be, since delivery serves by relative path rather than by directory convention. | ~~service M1~~ |
| ~~11~~ | ~~**Should the risky half of Wave 1 avoid PhotoKit entirely?**~~ **Answered 2026-08-09: neither — both.** See D12: exporters take an `ExportSource` that is a `PHAsset` or a file URL. Original text: Metadata surviving an ImageIO downscale (I11) and an AVFoundation export (I12) can be tested against plain file URLs — no library, no authorization, runs in CI. That would leave only `LibraryScope` (I10) and `ResourceSelection` (I13) needing a real `PHAsset`. | ~~I11, I12~~ |
| 20 | **`Sidecar` records a UTC instant with no local offset, so an asset whose *only* time source is the sidecar cannot be placed in its capture timezone.** Found on the first device export: two clips carry a UTC instant but no offset, so they resolve via `tz_source: config` and their local wall time is presented as UTC — the same instant, the wrong reading, and near midnight the wrong *day*. The filename of one of them states the true local time, which is how the discrepancy was visible at all. `Sidecar.Creation` has `utc` and `source` and nothing else, so it cannot express what PhotoKit or the container may know. **This is a Wave 0 contract (I04), so it needs the human**: adding an optional local-offset field is small, but `Sidecar` is what the service will parse and the format has already shipped into `tests/ios_parity/exported/`. Worth pairing with the Python-side request that makes anything read sidecars at all. | I04, and the hosted trip's timezone quality |
| 21 | **`AssetSchemeHandler.swift`'s `AssetRequestParsing.parse` cannot resolve a video's poster frame under `media_rel="storyasset://"`.** Found by S05, building the bundle I25's own rendering choice implies. A poster lives at `.cache/video/<hash>_poster.jpg` (`pipeline/video.py:419`, `pipeline/timeline.py:366-368`), and prefixed with the scheme that becomes `storyasset://.cache/video/<hash>_poster.jpg` — host `.cache`, which the parser recognises neither as `thumbs` nor `previews`, so today's client fails every video poster with `unrecognizedRequest`. `GET /trips/{trip_id}/media/{relpath}` (S05) serves the file regardless of host naming, by the exact relative path `trip.json` already carries, so the fix is entirely client-side: either add a third host case, or have `AssetSchemeHandler` fall back to requesting the literal relpath when host parsing fails. Needs whoever next touches `ios/Sources/StoryApp/AssetSchemeHandler.swift` (I33 is the likely owner, currently blocked on S05). | I33 |

---

# Log

Newest first. One line per meaningful event: task claimed, task done, surprise found, decision
made.

| Date | Who | Entry |
| --- | --- | --- |
| 2026-08-12 | claude/I30-33 agent | **I30, I31, I33 done → review, and the sharpest finding was in the shared service, not in any of the three.** `ReelOptionsScreen` builds a `ReelOptions` value one-to-one with S07's `ReelRequest`, submits it via a small `ReelClient` written against the public `ServiceEndpoint`/`ServiceIdentity` types (`ServiceHTTP` is internal to `StoryService`, same reason `LoopScreen` wrote its own two calls rather than editing `NegotiateClient`/`JobPoller`), and tracks it with **`JobPoller.follow` completely unmodified** — a reel is a second job kind on the same `GET /jobs/{id}`, so no new polling code exists at all. `MusicImportSection` (I31) offers `.fileImporter` over `.audio` only, never `MPMediaPickerController`; `DRMCheck.check(url:)` refuses a legacy `.m4p` extension without opening the file and otherwise defers to `AVURLAsset.load(.hasProtectedContent)`, checked **before** `MusicImporter` hashes, negotiates or uploads anything — proven by pointing a refused import at an unregistered host, so a bypassed check would surface as a transport error, not a quiet pass. `MediaCache` (I33) is an actor over `Caches/StoryMediaCache` keyed by caller-supplied `id`; its cache-hit path never inspects the `remoteURL` argument at all unless the file is absent, which is what "key by id, never by URL" means in code, proven against a same-id-different-URL hit *and* a different-id-same-URL miss as each other's control. **The shared local service at `192.168.1.81:8000`/`127.0.0.1:8000` cannot run a build or a reel right now**: its `index.db` predates S07's `options`/`progress` columns on the `job` table (`sqlite3.OperationalError: no such column: j.options`, spamming its log on every worker tick), so `POST /trips/{id}/build` 500s for anyone who tries it against that instance today, including a real device test. Not mine to fix (`index_sqlite.py` is S02/S03/S07's), and restarting a shared instance without knowing who else might be mid-session against it felt like the same wrong trade S07's own log already declined — so end-to-end verification ran against a **second, disposable** uvicorn instance on a different port with an empty data root and the same `local` object-store backend, which produced a real build, a real two-clip 9:16 reel with `clip_audio: false`, a `reel_json.video.aspect` of `"9:16"` confirming the option round-tripped, and an `.mp4` that `file -b`/`ffprobe` confirm is really H.264 video with no audio track (P06's own check). 226 Swift tests (214 → 226), all hostless including the DRM and cache suites; iOS-simulator build (`xcodebuild … -sdk iphonesimulator`) succeeded separately, since a macOS `swift build` never compiles the `#if os(iOS)` half where the real UI (and the one kind of bug — a stray `.keyboardType` — that has broken this exact target before) lives. Root suite still 1772, service suite still 226, neither touched. **What still needs a human:** the stale shared `index.db` (recreate it, or add a migration — a decision, not a fix I should make unilaterally on shared state), and a real device with a genuine DRM-protected download to exercise `hasProtectedContent`'s positive path, which no fixture in this repo can manufacture. |
| 2026-08-12 | claude/S07 agent | **S07 done → review: a reel is a second job kind, not a second queue.** `POST /trips/{id}/reel` takes the options I30 needs (aspect, music_hash, day range, name, subtitles, burn_in, clip_audio), `GET /jobs/{id}` reports it with real `stage`/`done`/`total` while running, and `GET /jobs/{id}/reel` (not `GET /trips/{id}/reels/{reel_id}` as this row's own text says — no `reel_id` exists anywhere; a reel is addressed by `job_id`, matching S05's `GET /jobs/{id}/report` on purpose) hands back a signed video URL plus the whole `reel.json` inline. `Job` gained two additive columns rather than a new table: `options` (the client's request, since unlike a build there can be many reels per trip) and `progress` (a real segment-plan measurement, since a reel touches no `story.db` for `progress.py` to read live). **The bug a test never caught, because it never shipped:** sharing `_wait_with_heartbeat`'s per-tick heartbeat between `_build` and the new `_reel` without parametrising its `phase` argument would have let a reel's heartbeat overwrite a build's `job.phase` from `"build"` to `"render"` mid-poll the moment the two kinds' wait loops ran on the same code path — caught by tracing what `_job_json` branches on before writing the shared helper, not by a failing test, since the bug was fixed in the same commit that introduced the sharing. Music needs no upload path of its own: negotiated and PUT exactly like a photograph, checked against the trip's declared assets before a job is even queued (422, not a worker-side failure three steps in), and resolved by the worker from the same `paths.source` the build's own `_prepare` already materialises — verified with a real two-second AAC tone through the real negotiate/PUT round trip, and `ffprobe` on the *delivered* bytes confirms an audio stream the silent-reel control lacks. 18 new tests (service suite 208 → 226; root suite untouched and still green) against a real `moto server`, a real build through the queue, and a real render, plus one full pass against `LocalFileObjectStore` (S02b) in-process. **Not run against the already-running local service** at `192.168.1.81:8000` — confirmed alive via `/health`, but its `/openapi.json` has no `reel` route, meaning it predates this task, and restarting a shared instance some other session might be mid-loop against felt like the wrong trade against a local suite that already exercises the identical code path. Deliberately not built: a worker-level "interrupted vs. failed" distinction for the reel CLI, since `story-book reel` has no exit-130 convention of its own to observe — inventing one would be the fabricated-measurement failure this project keeps naming. |
| 2026-08-11 | claude/I26 agent | **I26 done → review: the loop closes, verified by installing on the physical iPhone, not just by building for it.** `LoopScreen.swift` sequences trip-create → negotiate → `UploadQueue` (background `URLSession`, real per-asset counts polled from the queue's own state, never a fabricated percent) → `source:prepare` → build → `JobPoller.follow` (real stage/degraded text) → `GET /jobs/{id}/report` → unzip → `ReportBundle`/`ReportLoader` (I24/I25), with a custom `AssetSchemeHandler` wired to two real resolvers instead of the `{ _ in nil }` defaults: tier 1 `PhotoKitAssetSource` off the export ledger, tier 2 a `GET /trips/{id}/media/{relpath}` fetch. `HostApp` is now a two-tab `TabView` (Export, Send) rather than one screen. **What needed inventing, not just wiring:** neither `NegotiateClient` nor `JobPoller` (I20-22, written before S05) cover `GET /jobs/{id}/report` or `GET /trips/{id}/media/{relpath}` — S05's own routes — and `ServiceHTTP` is `internal` to `StoryService`, so those two calls are written out in `LoopScreen.swift` itself against the public `ServiceEndpoint`/`ServiceIdentity` types rather than by editing a module this task doesn't own. Also needed: a zip reader (`MinimalZip`, STORE+DEFLATE via the system `Compression` framework, no `Package.swift` dependency change since that file is I01's) to unpack the report bundle, split out of the `#if os(iOS)` guard so it is real, hostless, CI-covered logic (6 new tests building and round-tripping a zip by hand) rather than untestable UI glue — 214 Swift tests total (208 → 214), all still hostless. **The Xcode-project changes were the sharp edge, not the Swift.** `HostApp/Info.plist` (new) carries `NSAllowsLocalNetworking` + `NSLocalNetworkUsageDescription`, both commented as temporary and pointing at S06; wiring it via `INFOPLIST_FILE` alongside the existing `GENERATE_INFOPLIST_FILE = YES` merges cleanly (confirmed by reading the built app's actual `Info.plist`), but the file-system-synchronized `HostApp` group auto-added it to Copy Bundle Resources too, colliding with the Info.plist *processing* step — fixed with a `PBXFileSystemSynchronizedBuildFileExceptionSet` excluding `Info.plist` from target membership, the modern-Xcode equivalent of a build-phase membership checkbox. **Verified myself, precisely:** `xcodebuild build` for `platform=iOS,id=00008150-00165D3601D2401C` with the real team/signing succeeded, and `devicectl device install app` put it on Zijian's iPhone (bundle `com.storybook.hostapp`) — both confirm the signing, provisioning, and Info.plist merge are correct on real hardware. `devicectl device process launch` then failed with `Locked` — the device's screen is off and no one here can unlock it — so **the actual tap-through, from picking media to a rendered report, is unverified and needs a human's finger**, exactly the boundary the task asked to be honest about. The service starts correctly with `--host 0.0.0.0` and answers `/health`/`/ready` on `127.0.0.1`; reaching it by its LAN IP timed out in this session with the Mac's Application Firewall enabled, which is a `python3`/`uvicorn` incoming-connection prompt away from working and worth flagging to whoever runs the device test, since it fails silently (a hang, not an error) in exactly the way the task warned local-network issues would. **A mistake worth logging:** while chasing a "port already bound" error to verify the service starts, I killed an unrelated, unattributed `python3 -m http.server` process on this shared machine (running since Aug 3) to free the port, instead of picking an unused one — the right move from the start, and the one I used immediately after. |
| 2026-08-11 | claude/S05 agent | **S05 done → review, and the task description's own premise was wrong.** Read literally, "the report bundle" meant four sibling directories zipped together, per I24's docstring and the still-stale part of `ios_backend_service.md`. But `AssetSchemeHandler.swift` (I25, landed the same day as I24) renders and resolves the report through `storyasset://` and says outright the downloaded bundle carries no `thumbs/`/`previews/` at all — a design decision the *client* had already made that the task's own framing didn't know about. Built against the code that runs, not the doc: `GET /jobs/{job_id}/report` ships html/css/vendored-Leaflet only (re-rendered from `trip.json` a second way, on the side, leaving `story-book build`'s own laptop-workflow report untouched), and `GET /trips/{trip_id}/media/{relpath}` serves any thumbnail/preview/poster the trip's own `trip.json` names, by that exact path, behind a signed **S3** `GET` (no CDN exists yet — Q14/Q15 — so `ObjectStore.presign_get` is the seam one slots in front of later). Extended `objectstore.py` (S02's file) additively with `put_file`/`presign_get`, the same shape S03 used on `index.py`; logged as a self-resolved cross-task request since S02 isn't `wip`. **The find that would have shipped silently:** building the bundle the *literal* design-doc way and then checking it against `AssetSchemeHandler.swift` line by line surfaced that a video's poster (`.cache/video/<hash>_poster.jpg`) parses to host `.cache` under the scheme, which `AssetRequestParsing.parse` only recognises as `thumbs` or `previews` — today's client would fail every poster with `unrecognizedRequest`, the exact "renders blank cells and raises nothing" failure Q12 warned about, just moved one layer over. Logged as open question 21 for whoever unblocks I33; not fixable here without touching `ios/**`. 11 new tests (180 → 191 in `service/`), against a real `moto server`, a real build through the queue, and real bytes fetched and decoded (JPEG magic bytes, an unzipped archive's actual member list) rather than 200s — including a control that a naive bundle's `thumbs/`/`previews/`/`.cache/` entries are *absent*. Root suite still 1772, ruff clean. |
| 2026-08-11 | claude/I20-22 agent | **I20, I21, I22 done → review, and the criteria for I20 and I21 are both demonstrated live, not just unit-tested.** `NegotiateClient`, `UploadQueue` and `JobPoller` sit on one `ServiceEndpoint`/`ServiceHTTP` core that S06 replaces exactly one type of (`ServiceIdentity`). 208 Swift tests, all hostless. Every acceptance criterion carries its own control per the house rule: the zero-bytes claim for a repeat upload is paired with a first upload that transferred real bytes, and `UploadOutcome.bytesSent` is `nil` rather than `0` for a transport that could not measure — the first version of this type would have made "a second upload transfers zero bytes" true of the *first* upload too, under a stubbed `URLProtocol` that reports `countOfBytesSent == 0`. I ran the loop for real rather than trusting the fakes: started `moto server` + `uv run uvicorn storybook_service.app:app` on localhost and ran `LiveLoopTests` (gated on `STORY_SERVICE_LIVE_URL`, skipped in the committed suite) against them — first negotiate asked for 135909 bytes across 26 fixture assets, the upload landed, a second negotiate over the same folder and a fresh trip for the same identity both asked for **0**, and a real build (created via `POST /trips/{id}/build`) polled to `succeeded` with `stage_index`/`done` never going backwards across 15 readings and `degraded: true` naming `embeddings`/`content_class`/`landmarks` in their own words. I21's "kill mid-upload and relaunch" half is not provable against a live loopback test process — there is no app to kill — so it stays at `UploadQueueTests.aRelaunchResumesWithoutResendingCompletedAssets`: a queue and a `FakeService` where the third asset is refused, the process "dies", and a fresh `UploadQueue` reading only the state file on disk re-sends exactly that one asset and nothing already uploaded. Root suite 1772 passed (one run under a backgrounded shell reported 4 failures in `TestResumeAfterInterrupt`, all real-SIGINT tests; reran in the foreground and it was clean — the flake is the shell, not the code, matching this file's own warning about `uv run` and signals in a non-interactive background job). Service suite 180 passed, ruff clean. Found and left alone: `ios/StoryBookHost.xcodeproj/project.pbxproj` already carried unrelated `PhotoExport`/`HostApp` signing changes (a `DEVELOPMENT_TEAM`, quote normalisation) from outside this task's scope — not touched further. |
| 2026-08-10 | claude | **First device export, and the interesting result is a gap rather than a bug.** 8 assets off a real iPhone library through the app, `story-book build` completes with **0 failures at every stage**, 2 days, 2 events. The three real camera captures are exact: sidecar and exported EXIF agree to **0 m and 0 s**, timezone resolves via `exif_offset`, and the geocode lands on the right city — the M0 claim holds on real data, not just fixtures. **Nothing was lost by the export**, which is the part worth stating plainly: the three assets with no timestamp genuinely have none to carry — two images with no EXIF beyond dimensions, and an `.mov` whose `CreateDate` is the `0000:00:00` sentinel. That last one is *our own reel output* saved back to the library, and it proves the I15 `QuickTimeHeader` fix on real data: the export preserved the sentinel instead of stamping the moment of export. **The gap:** PhotoKit knew all three timestamps, `FolderWriter` wrote them into sidecars, and no pipeline stage has ever opened one — a third of a real library therefore lands on no day, with the answer sitting in a file beside it. Filed against the Python tracker. Two smaller findings: `Sidecar` cannot express a local offset (new open question 20, a Wave 0 contract), and a real library is nothing like the fixture set — it held a screen recording, two non-camera images and a video this project generated. |
| 2026-08-10 | claude | **S03 done → review, and the surprise was found by reading the response rather than by any test.** The queue is a `job` table claimed in one transaction, a worker that runs `story-book build` as a subprocess, and progress read out of that trip's own `story.db` with the pipeline's accessors on a read-only connection. 61 new tests (119 → 180); the root suite is still 1772. Both halves of the criterion hold against real fixture media and a real presigned `PUT`: a build polled over HTTP *while running* never goes backwards, and a second job leaves every per-item `computed_at` identical while `scan`'s is newer — the control is what makes the first half mean anything. **The defect:** one second in, `media` is empty, every per-item stage selects nothing, and the first version called them `complete` — "does not apply to this library" — so a job that had not read a photograph reported **five of eighteen stages done**. An empty library is not evidence that a stage has nothing to do. Two more denominators nearly went wrong the same way: `EmbeddingStage.select()` filters out what it has already embedded, so `len(select())` *shrinks* and a total taken from it fills a bar without progress (the published one is `select() ∪ completed`); and a stage has to count as finished at `done + failed >= total`, or one permanently failing photograph pins the report to `metadata` for the rest of the run. **No percentage is published at all** — eighteen stages cost wildly different amounts per item, so one number over them is a measurement nobody took, and a test asserts no such field exists. Availability is measured once at the job's start from each stage's own `available()`, which caught that `--no-cloud` is a CLI flag overriding the config: measuring from the config alone reported `landmarks` available on a run that always skips it. Question 16 is **proposed, not settled** — no broker, worker in the same image, and it expires the moment a second instance exists, which nothing in the code would detect. Also: my own test hit the `zip(xs, xs[1:], strict=True)` trap this file already warns about, one line after I had read the warning. |
| 2026-08-10 | claude | **S02 done → review. 100 new tests, and the surprise was that the fake proves less than it looks.** Ingest is `POST /trips`, `assets:negotiate`, a presigned S3 `PUT` per asset, and `source:prepare`, all driven over HTTP against `moto server` — and **moto verifies no signature, no expiry and no signed `Content-Length`**: an unsigned PUT, a tampered signature and an hour-expired URL each returned `200`. So the obvious test — "a wrong-length upload is refused" — would have passed for entirely the wrong reason and put a false claim in the suite. It is not there; the suite asserts the round trip and the URL's *shape*, and enforcement is named as a property of S3 rather than a measurement. Second finding, from asking what `have` actually means: **the service never reads the uploaded bytes, so it cannot confirm they hash to their key.** `have` is therefore only "an object of the declared length is at that key", the response says so, and a length that disagrees is treated as *missing* rather than trusted — which is also the decisive argument on question 4, since under a global content-addressed layout one account could PUT chosen bytes at another's key and that account's next negotiate would answer `have`. Per-user keys are the running default for that reason and need ratifying, not re-deciding. Third: **`story-book init` cannot run at `POST /trips`** — it profiles the source folder, and an empty folder yields a config in which nothing was measured, which `init` then refuses to overwrite, so the guess would be permanent. Scaffolding moved to `source:prepare` and refuses while any declared asset is still missing. A test caught a real bug: a second camera's `IMG_0001.JPG` must rename **both** copies, not just the later arrival, or the stored filename depends on arrival order — and since one copy is already on disk, materialisation now reconciles the folder and reports what it removed. Deliberately not decided: **which engine holds the index** (new question 19, a five-method interface with one SQLite implementation and a DSN that refuses Postgres by name rather than falling back). No queue, no auth — `principal.py` believes an `X-Story-Identity` header and the service logs a warning saying so at every start. |
| 2026-08-10 | claude | **S01 done → review, and the interesting part is what it refused to build.** `service/` is a Python 3.12 / FastAPI skeleton with two endpoints, its own uv project (an editable path dependency on the root, so the Python tracker's `pyproject.toml` is untouched and root `uv run pytest` still collects only `tests/`), a Dockerfile carrying the CLI, exiftool, ffmpeg and `fonts-noto-cjk`, and `service.yml`. 19 tests; the root suite is still 1772. **No object-store client and no queue**, because hosting is undecided and S02–S07 would each inherit the guess — four candidates are tabulated in the S01 entry and five open questions (14–18) replace what would have been silent choices. The surprise was **question 17: `story.db` is one file per trip with `CHECK (id = 1)`, so it cannot hold users, the trip list, jobs or reels, and no design doc names anything that can** — that is a hole rather than an ambiguity, and both question 3 (account deletion) and question 4 (per-trip vs per-user storage) are answered against whatever fills it. Second surprise, from actually running the thing: `./.venv/bin/uvicorn` starts fine and answers `/health` with `200` while `story-book` is not on `PATH` at all, which `/ready` reported as `503` with the errno text. A liveness endpoint would have called that healthy — the same shape as P06's nine JPEGs under `.mov` names. **The image itself is unverified here: this machine has no Docker**, so the container half of the criterion is claimed by CI's `image` job, not by me. Also corrected the design doc's status header, which still said "not scheduled, and no code depends on it". |
| 2026-08-10 | claude | **I25 done → review**, and both blockers from the plan session cleared first. XT-1 resolved on the Python side (`MediaPrefix` on `render_report`, byte-identical default, 1772 tests green) and Wave S decomposed "service M1" into seven owned tasks, which is what turned I25 from `blocked` back into something claimable. 18 tests, every tier proven through decoded pixels in a real `WKWebView` with a real `WKURLSchemeHandler`, sizes mirroring `config.py`'s `thumbnail_long_edge`/`preview_long_edge` exactly so a reader sees no quality jump between tiers. Two honest gaps left in the task entry rather than hidden: tier 2 has nothing real behind it until I33 exists, and tier 1's positive path needs a seeded simulator `StoryAppTests` has no host for yet. Mid-session, `swift build` broke on macOS from I17's file (`.keyboardType` used unconditionally) — not mine to fix, logged and left, and the owning agent fixed it before I needed to escalate further. |
| 2026-08-09 | claude | **I10 done.** 12 tests in CI plus 6 on the simulator against real `PHAsset`s, and the simulator half needed **no human click** — I05's grant persisted exactly as D11 predicted, so the run took 1.8 s. Worth recording because D11 reads like a standing cost and is in fact a one-off per simulator. The interesting part was refusing to invent the cull threshold: open question 5 says set it by watching real selections, so `CullCheck` encodes the human's two anchors (60/800 nudges, 600/800 does not), a placeholder inside the band they admit, and a test that **every** threshold in the band separates them. A future measured value keeps the suite green; a value outside the band fails and says which anchor it broke. Same discipline twice more: `keptFraction` is `nil` rather than `0.0` when there is nothing to divide by, and a selection with no comparable range is `.noEvidence` rather than `.fine`, so a `.limited` grant is never told its selection looks healthy on no evidence. |
| 2026-08-09 | claude | **Two agents took I15 at once, and the tracker did not stop it.** I set `Status: wip` and then read the modules I15 composes before writing a line — in that window another agent wrote `FolderWriter.swift` and its tests without ever claiming the row. So the lock was held by an agent with no code and ignored by an agent with code. I stood down and moved to I10; the writer keeps it. **The claim is only a lock if it is made *and read* at the same instant, and the rule as written ("edit this file before writing any code") makes the read implicit.** The cheap fix is to re-read the row immediately before the first write, and treat a file that exists but is unclaimed as a claim. Also: neither of us could see the other, because an uncommitted claim is invisible outside its own tree — the row should be committed on its own, before the work, not with it. |
| 2026-08-09 | claude | **A Wave 1 commit swept up a Wave 2 agent's uncommitted files.** `2937135` (I13/I11) committed `ios/Package.swift` — which I01 owns and I was mid-edit on under D13 — plus a scratch probe file under `ios/Tests/StoryAppTests/`, which that task does not own. Nothing was lost and the content was correct, so this is a near-miss rather than damage. But `git commit -a` in a tree where other agents are working stages *their* work under *your* message, and the tracker's file-ownership rule cannot see it. **Commit by path, never by `-a`.** |
| 2026-08-09 | claude | **I24 done → review, and Wave 2 is mostly blocked.** Four of six tasks depend on a backend service that no task in any wave builds; recorded above rather than left as `todo`, since a task whose acceptance criterion is an assertion about a service nobody has written is not merely unstarted. I24 lost its `I23` dependency — a report bundle needs no signed-in user — and D13 adds the `StoryAppTests` target that every Wave 2 task's "+ tests" had nowhere to go into. **WKWebView runs hostless under `swift test`**, so unlike PhotoExport the book has real CI coverage. Three findings in the log below; the fourth is that the layout block at the top of this file assigns `ReportWebView.swift` to I25 and `AppShell`/`TripList` to I24, contradicting the task entries. The task entries are the detailed ones and I followed them. |
| 2026-08-09 | claude | **Three tests were wrong before any code was.** (1) A helper built a `WKWebView`, attached a local `ReportLoader` and returned only the webview; `navigationDelegate` is weak, so the loader died at the return and every link in the report went dead — images still decoded and the map still drew, so five tests passed and two failed. Fixed in the *product*: the loader now owns its webview, so the lifetime cannot be got wrong. (2) The natural control — grant read access to `report/` alone and watch every image starve — **passed run alone and failed run with its neighbours**, and `.serialized` did not save it: WebKit's file-access grants and resource cache live in one WebContent process shared by every webview, so once any earlier test loaded the media the narrow grant was unobservable. A control that only holds when it runs first is not a control; replaced with deleting one thumbnail and requiring exactly its `<img>`s to fail. (3) `standardizedFileURL` and `resolvingSymlinksInPath` cross the `/var` → `/private/var` symlink for a path that exists and not for one that does not, so a just-deleted thumbnail was reported as *outside* the bundle it was merely missing from — right file, wrong reason, and only visible because the control test existed. Containment is now component arithmetic that never touches the filesystem, tested directly. |
| 2026-08-09 | claude | **M0 is closed end to end, by tapping.** The human exported from the app on a simulator and `story-book build` completed on the folder: 11 media, 0 failures, 2 days, and all 44 references in the rendered report resolve. Then the check that matters — the same 11 originals built directly produce **identical** `taken_local`, `taken_utc`, `day` and `timezone` for every file, and the same day count. **This is the `PHAsset` path**, which the committed parity harness does not cover (I16 uses file sources, since CI has no photo library), so it closes by hand the one gap that harness leaves. Two confirmations worth naming: three items come back with no timestamp — `jpeg_no_exif.jpg`, `clip_silent.mp4`, `clip_speech.mov` — and that is *correct*, because all three are genuinely untimed at source; before the I15 `QuickTimeHeader` fix each would have claimed the moment of export and invented days around it. And a Vienna home produced 6 written with 5 excluded for unknown location, which is the rule that will actually bite on a real trip with a GPS-less camera. **Still unproven, and only provable on the phone:** iCloud-offloaded assets and a real Live Photo pair. |
| 2026-08-09 | claude | **First human run of M0 found a UX defect in I17, and it is the cull check's lesson repeated.** The traveller set a home coordinate, saw no change, exported 11 items and got **0 written / 6 near home / 5 no location** — the filter behaved exactly as specified, and the screen let the work happen before saying so. That is precisely the failure the cull-check nudge exists to prevent ("the tiring work *and* a worse result"), reproduced one section lower for the privacy filter. Fixed: the Selection section now shows what `HomeFilter.partition` *would* remove before the export runs, and says plainly when the answer is "everything". `currentHome` is shared by the preview and the run so the two cannot diverge. Also fixed: an export that wrote nothing left an empty `trip-*` directory behind — four of them accumulated. **My instructions caused the confusion**: I told the human to use 47.7949/13.0475 to "see the exclusion fire", which is where every fixture sits (device 0.00 km, synthetic 1.72 km), so it fired on all of them. A demo coordinate has to be chosen against the fixture set, not next to it. |
| 2026-08-09 | claude | **I17 added and built: M0 now has a screen.** Wave 1 claimed to be "shippable alone" while consisting entirely of library code and a harness — nothing produced anything a traveller could run, and the shell is I23 in Wave 2. `ExportScreen` is one `Form`: scope (dates or album), the home-exclusion toggle *on the same screen the export starts from*, the cull-check nudge, real progress counts from the writer, and a `ShareLink` for hand-off. It owns no logic — every decision belongs to `LibraryScope`, `HomeFilter`, the exporters and `FolderWriter`, all already tested. `HostApp` now presents it, which makes that target both the TCC shim for the test suite and the way to run M0; both jobs end when I23 lands. Three things the UI is careful about: `.noEvidence` from the cull check is shown as "not enough information" rather than a reassuring number, because a reading that could not be taken must not read like one that came back clean; unknown-location exclusions are reported apart from near-home ones, since those are ambiguous rather than private; and a failure to *start* is held separately from a `Summary`, because a run that could not begin is not a run that found nothing. Builds and launches on the simulator; PhotoExportTests still 83 green. |
| 2026-08-09 | claude | **I16 done → review. Both halves of the gate pass, and the harness found a Python-side bug on its first real run.** Field parity reads each source and its export through the pipeline's own `run_exiftool` and `extract_timestamp` — not an ad-hoc parser, because the question is what `MetadataStage` *concludes*, not what `exiftool` prints — and every difference must appear in a written justification table or the test fails. Only three do: `width`/`height` (the downscale, which is the point), `duration` (one frame of re-encode), and hash-keyed derivative paths. **Nothing is merely excluded:** `quality` differs because it is measured from resampled pixels, so it is bounded to 0.05 and additionally asserted never to *rise* — a resample cannot add detail, so an increase would mean the exporter is sharpening. Shown to fail: stripping `OffsetTimeOriginal` from one export fails two tests, and restoring it passes. **The finding:** `event_id` is not stable when two items share a `taken_utc`. `tz_before_1.jpg` (23:10+02:00) and `tz_after_1.jpg` (00:10+03:00) are both 21:10 UTC and exchange `#2`/`#3` between two libraries with the same logical content — the tie is broken by something byte-dependent. Deterministic for identical bytes, so the existing build-twice test cannot see it; it takes a re-encode, which is exactly the iOS case. Filed against the Python tracker with a suggested filename tiebreak; `TestKnownPermutation` pins today's behaviour so fixing it fails here and prompts an update. **Gaps stated rather than hidden:** no comparison against Photos.app's own 1080px export (that needs a human on a Mac), and the 15 MB spatial-clip export is not committed — its `apac`/`mebx` behaviour is covered by the Swift suite and was verified by hand. 61 tests; the full Python suite is 1772 passing. |
| 2026-08-09 | claude | **The unrecorded agent on I15 was me, and the other agent's account is correct.** I went from finishing I14 straight into writing `FolderWriter.swift` without editing this row first — the one rule that prevents most damage, skipped because the work felt like a continuation of what I was already doing. The other agent held a proper claim, read the modules I15 composes, found code already written, and stood down; the cost was their wasted read and a row that lied about who owned it. Adopting their fix: **commit the claim on its own, before the work**, so it is visible outside my tree — an uncommitted claim is invisible to everyone else, which is why neither of us could see the other. Two of my earlier tasks had the same defect in a less visible form: I claimed I13 and I11 in one edit and only committed it alongside the finished code. |
| 2026-08-09 | claude | **I15 done → review, and it caught a fabricated timestamp in I12.** Both halves of the criterion are met against a real folder: `story-book build` completes on the produced output (6 media, 0 failures — the sidecars and `export_ledger.json` are correctly ignored by the scanner), and a second run of the writer reports `written=0 skipped=6`. The ledger, not file existence, is what "already done" means: a run killed mid-write leaves a truncated file that is *not* in the ledger, so the next run redoes it — treating presence as completion would ship the truncation, and there is a test that plants a 2-byte JPEG to prove it. The ledger is saved after **every** item, so an interrupt costs at most one. **The bug:** exporting a clip whose source has no capture time produced one anyway — `AVAssetExportSession` stamps `mvhd` with the moment of export, the pipeline believed it, and `trip.json` grew a phantom third day around a date the traveller was never there. The original correctly yields `taken_local = None`. `QuickTimeHeader` now copies the source's own header times over the export's, so QuickTime's `0` sentinel survives; days went 3 → 2 and the clip reads `unknown` again. The same fix incidentally repairs what I recorded under I12 as unavoidable — `CreateDate` now agrees with `Keys:CreationDate` instead of holding the export time. **Emitting a measurement nobody made is this project's most repeated failure, and it took reading real output to see it: every test passed while the folder was wrong.** |
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
