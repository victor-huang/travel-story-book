# iOS client — implementation plan

> **Status: draft, optional, not scheduled.** Nothing here is claimed in
> [`implementation_tracker.md`](./implementation_tracker.md), and no code depends on it.
> Created 2026-08-09.
>
> [`ios_client_app.md`](./ios_client_app.md) is the **what and why** for the app. This is the
> **how**: language, module layout, task order, and how any of it gets tested. Companions:
> [`ios_backend_service.md`](./ios_backend_service.md), and
> [`reel_on_ios_avfoundation.md`](./reel_on_ios_avfoundation.md).

---

## Decision: Swift 6 + SwiftUI, native, minimum iOS 17

Taken 2026-08-09. Not a close call, for one reason: **every hard part of this app is an Apple
framework call.** The risk lives in PhotoKit (`PHAsset`, `PHAssetResource`), ImageIO
(`CGImageSource` → `CGImageDestination` with properties copied) and AVFoundation
(`AVAssetExportSession` with metadata carried over). A cross-platform toolkit abstracts the easy
half — layout, lists, networking — and forces native modules for exactly the half that is hard.

Worse, those abstractions fight the acceptance criterion directly. M0 stands or falls on metadata
surviving a downsized export, and **stripping EXIF on resize is the default behaviour of most
cross-platform image pipelines.** The same is true of QuickTime creation date and location
through a video export.

And there is no shared-code upside pulling the other way: the pipeline stays Python on the
server, so nothing is shared with the client in any language.

**iOS 17, not 18.** Nothing here needs the newer Vision APIs — those belong to the on-device
pipeline, which is deferred and may never be built. PhotoKit, ImageIO and AVFoundation are
long-settled, so 17 widens reach for free.

**Android, noted and deferred.** Because the pipeline is server-side, an Android client would be
a separate exporter against the same source-folder contract, not a port. Kotlin Multiplatform
could share upload and curation logic but not the export path, which is the expensive part. Not a
reason to avoid Swift now.

---

## Dependencies: aim for none, with one known exception

Everything the app needs ships with the platform — except the content hash.

**`hashlib.blake2b()` with default parameters is a 64-byte digest, hex-encoded to 128
characters** (`pipeline/scan.py:45`), and `asset_id` is a prefix of it. The client must reproduce
that digest byte for byte or hash negotiation silently fails to match and every asset re-uploads.

**CryptoKit does not provide BLAKE2b.** Confirm the same for `swift-crypto` before assuming
either way; if neither exposes it, the options are a small vendored Swift implementation or
libb2 through C interop. Whichever is chosen, it needs a test vector check against the Python
side — the same file hashed by both must produce the same 128 characters, asserted in CI rather
than by inspection.

This is the first task of M1 and it is small, but it is load-bearing: identity is the pipeline's
whole model.

---

## Module layout

Four targets, split so that the risky code is testable without a UI.

```
StoryKit        pure logic, no UIKit/SwiftUI, no PhotoKit
                  ContentHash        BLAKE2b, chunked
                  TripDocument       decode trip.json (read-only view of the contract)
                  OverridesFile      write overrides.toml
                  Sidecar            per-asset metadata JSON
PhotoExport     PhotoKit + ImageIO + AVFoundation
                  LibraryScope       album / date range / multi-select → [PHAsset]
                  StillExporter      1080px, properties copied
                  ClipExporter       1080p, metadata carried
                  HomeFilter         coordinate exclusion, pre-upload
StoryService    URLSession client: negotiate, upload, jobs, artifacts
StoryApp        SwiftUI
```

`StoryKit` and `PhotoExport` hold everything that can be wrong in a way tests can catch.
`StoryApp` should stay thin enough that no logic hides in a view.

### Repo placement

Start in this repository under `ios/`. The contract, the fixtures and the parity harness all live
here, and a second repo means the app and the format it targets drift independently. CI gets a
separate macOS workflow; the existing Python jobs are unaffected. Split later if it ever earns
its own release cycle.

---

## Task order

Dependencies matter more than the numbering.

### M0 — the exporter, no backend

| # | Task | Depends on |
| --- | --- | --- |
| 1 | `LibraryScope`: authorization, album/date-range/multi-select → `[PHAsset]` | — |
| 2 | `StillExporter`: `requestImageDataAndOrientation` → ImageIO downscale, properties copied | 1 |
| 3 | `ClipExporter`: `AVAssetExportPreset1920x1080`, creation date + location set explicitly | 1 |
| 4 | Filename resolution from `PHAssetResource.originalFilename` | 1 |
| 5 | `HomeFilter` applied before anything leaves the device | 1 |
| 6 | Write the folder; hand off via Files / AirDrop | 2, 3, 4, 5 |
| 7 | **Parity harness** — the gate; see below | 6 |
| 8 | Sidecar JSON per asset (`creationDate`, `location`, source of each) | 1 |

Tasks 2 and 3 are the ones to start on. They are where the project's risk is concentrated, and
they are testable long before there is an app around them.

### M1 — upload and build

Content hash + test vectors → negotiate endpoint client → background `URLSession` with per-file
retry → job polling → render `trip.json` as a browsable trip.

### M2 — reel

Render options (aspect, music, day range) → music import from Files, **not** the Apple Music
library → `AVPlayer` against a signed URL → share sheet.

### M3 — curation

`trip.json` proposal grid → `pin`/`reject` as one-tap gestures → alternates strip from dedup
clusters → `OverridesFile` writer → re-run → push notification.

### M4 — story

Nothing in the app beyond displaying prose the service obtained.

---

## Testing

The house rules apply unchanged: one behaviour per test, real fixtures over mocks for anything
touching media, and **a test of a failure mode must be shown to fail.**

### The parity harness is the important one

M0's entire claim is *the app produces the same shape of file Photos does*. So:

1. Export a set of assets from the app.
2. Export the same assets from Photos on the Mac at 1080px.
3. Compare **field by field, justifying every difference.**

Two details that make this stronger than a generic `exiftool` dump:

- **Read both sides with the pipeline's own metadata code**, not an ad-hoc parser. The question
  is not "what does exiftool see" but "what will `MetadataStage` conclude", and those differ
  wherever the pipeline resolves rather than reads.
- **Then run the pipeline on both sets and diff `trip.json`.** Asset ids *will* differ, because
  the bytes differ — so compare structure, not identity: day count and boundaries, event count,
  timestamps, resolved timezone offsets, places. A difference there is the export losing
  something a field-level diff let through.

iOS-exported fixtures should join `tests/fixtures/media/` as a second producer of the contract.

### Simulator is not enough for the export path

The simulator's photo library has no HEIC with GPS *and* an offset tag, no burst, no Live Photo,
no spatial-audio `.mov`. Either seed a library via `PHPhotoLibrary` from the committed fixtures
or run the export tests on a device. Deciding this early avoids a suite that passes everywhere
and proves nothing — the same failure as HEIC decoding "working" for months because the test
registered the opener itself.

### Contract tests that run on the Python side

- **Hash agreement.** One file, hashed by `ContentHash` and by `hashlib.blake2b`, same 128 hex
  characters.
- **Overrides round trip.** `OverridesFile` output loaded by `Overrides.load`, asserted non-empty
  and fully resolvable. An override naming nothing is an *error* by design, so a generator that
  emits one must be caught here rather than at the user's next build.
- **`trip.json` decoding.** `TripDocument` parses the committed artifact, so a schema change
  breaks the app in CI rather than in someone's hands.

### CI

A macOS workflow building and testing `StoryKit` and `PhotoExport`. Pin actions to exact
versions — `releases/latest` tells you the version, not which refs resolve.

---

## Risks

- **BLAKE2b availability in Swift** (above). Cheap to resolve, expensive to discover late.
- **Background execution.** `BGProcessingTask` is opportunistic with no guarantee and jetsam
  kills on memory. Export and upload must survive being interrupted repeatedly.
- **Memory during export.** Stream per asset; never hold a batch of full-size images.
- **Asset variants.** Live Photos, slow-mo, cinematic mode, RAW+JPEG pairs each have a "which
  resource do I export?" answer, and a wrong one yields a plausible file with wrong timestamps.
  Enumerate them; do not discover them.
- **Swift 6 strict concurrency** across PhotoKit callbacks and `AVAssetExportSession` is real
  work in itself. Budget for it rather than fighting it at the end.

---

## What the app treats as frozen

Changing any of these breaks the app silently, so they go through the tracker's cross-task
request table like any other contract:

- the source-folder shape (1080px stills, 1080p clips, metadata, original filenames),
- `trip.json` and its schema,
- the `overrides.toml` vocabulary and its filename/asset-id addressing,
- `reel.json` as the honest record of what a render actually did.

---

## Open questions

1. **Does `swift-crypto` expose BLAKE2b?** Decides vendor-vs-interop, and it is task one.
2. **Device or seeded simulator** for the export test suite?
3. **Does `ios/` stay in this repo** past M1?
4. **Xcode project or Swift Package for the app target** — a package keeps `StoryKit` and
   `PhotoExport` buildable from CI without an app scheme, which is worth something.
