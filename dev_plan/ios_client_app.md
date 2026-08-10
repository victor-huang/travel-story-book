# iOS client — the app that produces the trip and curates the result

> **Status: draft, optional, not scheduled.** Nothing here is claimed in
> [`implementation_tracker.md`](./implementation_tracker.md), and no code depends on it.
> Created 2026-08-09.
>
> Companions: [`ios_client_implementation.md`](./ios_client_implementation.md) (the **how** —
> language, modules, task order, tests), [`ios_backend_service.md`](./ios_backend_service.md)
> (the hosted half — ingest, jobs, delivery, retention) and
> [`reel_on_ios_avfoundation.md`](./reel_on_ios_avfoundation.md) (an eventual on-device renderer,
> deferred). This doc is the **what and why** for the app.

The laptop workflow is unchanged. `story-book build` against a local folder stays the reference
implementation; the app is another way to produce the folder it reads.

---

## What the app is

Three things, in order of how much they matter:

1. **An exporter.** It turns a rough scope of the user's photo library into the source folder the
   CLI already accepts: 1080px stills and 1080p clips, metadata intact, original filenames kept.
2. **A curation surface.** It shows what the engine proposed and lets the traveller nudge it —
   a few added, a few removed. That correction is `overrides.toml`, which already exists and is
   already stable.
3. **A player.** The finished reel, plus the report.

The governing principle, which every screen is judged against: **the machine proposes, the person
nudges.** Effort spent choosing photographs is effort the app failed to absorb.

**What it is not: a second pipeline.** It computes no days, no events, no clusters, no selection.
Every decision about what exists comes back from `trip.json`. The same rule the story overlay
follows — *a story is an overlay, never a source of structure* — governs the app.

---

## The point of the app: nobody should have to pick the good photos

Choosing the best fifty frames out of eight hundred is the work the traveller does not want to
do, and it is exactly the work the pipeline has already been built to do. So the app's job is to
**take a rough set in and hand a proposal back**, then make correcting that proposal as close to
free as possible.

```
rough scope in  →  export + upload  →  the engine proposes
     →  traveller adds a few, removes a few  →  re-run  →  book and reel
```

### Rough is fine. Culled is not.

Any low-effort way of saying *roughly this much* is welcome, and the app should offer all of
them:

- a date range ("the trip was the 14th to the 22nd"),
- an album or a Photos "Trip" memory,
- a quick multi-select drag across the grid — hundreds of items in a gesture.

What breaks the pipeline is not *how* the set is chosen but whether it has been **thinned**.
Event detection segments on time and location gaps, and dedup clusters bursts; both read the
stream, not the highlights. A set already reduced to favourites comes back with gappy events,
nothing to deduplicate, and a selection stage with nothing left to select from — and the user
will have done the tiring work *and* got a worse result for it.

The distinction is measurable, so the app should measure it rather than lecture: compare the
selected count against how many assets exist inside the range the selection covers. Sixty of
eight hundred in-range is a cull; offer *"include all 800 from these days — you won't have to
choose, that's what the book does"*. Six hundred of eight hundred is someone dropping obvious
junk, which is harmless.

Undershooting is recoverable and overshooting is nearly free, so the nudge always points the
same way: **when in doubt, include it.**

---

## Reading the library

**`PHAsset` via full library read authorization, not `PHPickerViewController` alone.**

`PHPicker` is the privacy-friendly default and it is not enough here. The pipeline wants
`creationDate`, `location`, burst identifiers and Live Photo pairing, and those are properties of
the asset in the library, not of a file handed out by a picker.

There is an upside that pays for the extra permission prompt: `PHAsset` gives an absolute
timestamp *and* coordinates directly. Timezone resolution is the highest-risk logic in this
project — `pipeline/timezones.py`, plus a whole retro on offset-vs-GPS conflicts having two
causes — and the phone can simply hand over the answer. **Send a per-asset sidecar JSON and let
the server prefer it over EXIF inference**, recording which source won.

---

## The export, which is where the bugs will be

This is the app's real work. Everything downstream is already proven against files of this shape.

### Stills

`PHImageManager`'s resized-image requests return a `UIImage` with **no EXIF at all**. The correct
path is `requestImageDataAndOrientation` for the original data, then downscale through ImageIO,
copying the source's properties into the `CGImageDestination`.

Fields that must survive, and why:

| Field | Consumer |
| --- | --- |
| `DateTimeOriginal` | every day/event boundary |
| `OffsetTimeOriginal` | first branch of timezone resolution |
| GPS lat/lon | geocode, home filter, map, gps_backfill |
| Orientation | `geometry.orientation`, reel framing |
| Make/Model | the same-device neighbour rule in `timezones` |

### Video

`AVAssetExportSession` at `AVAssetExportPreset1920x1080` does **not** carry QuickTime creation
date or location unless set explicitly. Note that the fixture set already contains a
Photos-export-shaped `.mov` whose `CreateDate` disagrees with its `Keys:CreationDate` — that
disagreement is a property of how an exporter writes the file, and an AVFoundation exporter will
produce its own variant. Assume it differs; do not assume it matches Photos.

### Filenames

**Preserve `PHAssetResource.originalFilename`.** `overrides.toml` addresses media by filename or
asset id, so `IMG_1815.mov` staying `IMG_1815.mov` is what lets corrections and habits carry
across from the laptop workflow.

### The acceptance gate — this is M0's whole point

Export a set of assets from the phone and the *same* assets from Photos on the Mac. Run
`exiftool` on both and diff **field by field, justifying every difference.** Not "the file has
EXIF"; not "the build succeeded".

This is the one test that cannot be replaced by anything cheaper, because the entire argument for
this architecture is *the app produces the same shape of file Photos does*. If that holds, 1700
existing tests already cover what happens next. Fixtures produced by the iOS exporter should join
`tests/fixtures/media/` as a second producer of the same contract.

---

## Privacy: the home filter runs on the phone

`home_filter` runs before *export*, which is after ingestion — fine when ingestion is a local
folder, useless once bytes have left the device. The coordinates are on the phone, so the
exclusion is applied **before upload**, and excluded media is never transmitted.

This is the only place the hosted model is materially weaker than the laptop one, and applying
the filter client-side is what keeps it from getting weaker still.

---

## Curation, and how light "light" actually is

### The effort budget, from the real trip

The Vienna/Munich library is 274 assets in `trip.json`, of which the pipeline selected 64 — 47
day highlights and 9 trip highlights. Against that, the labelling exercise behind
`overrides.example.toml` found **19 hand-made decisions that no threshold setting could
reproduce**: the best combination of quota, spacing and diversity recovered 7 of 16 requested
photos, and only by doubling the size of the book.

Read together those two numbers set the target. The traveller reviews **tens of items, not
hundreds**, and makes **roughly twenty decisions on a nine-day trip**. That is the budget. A
design that asks for more has moved the burden back onto the person the app exists to relieve;
one that asks for none is pretending thresholds can do a job the data says they cannot.

### Doing nothing is a valid answer

The proposal must be good enough to ship untouched. Curation is an improvement, never a gate —
no "review 47 photos to continue" step, and no empty state that blocks the book.

### Two gestures in front, the rest behind

| Gesture | Override | Surface |
| --- | --- | --- |
| I want this one | `pin` | **Primary** — one tap on the grid |
| Not this one | `reject` | **Primary** — one tap on the grid |
| Use this frame instead | `keeper` | One tap, from the alternates strip |
| These are two different stops | `split_event` | Secondary |
| This is all one thing | `merge_events` | Secondary |
| Call this stop something | `label_event` | Secondary |
| Call this place something | `label_landmark` | Secondary |

Three things that keep the two primary gestures cheap:

- **Review the proposal, not the library.** The default surface is the ~50 selected items grouped
  by day, not 274 and certainly not 800.
- **Put the alternates one tap away.** The commonest real correction is *"right moment, wrong
  frame"*, and dedup already knows the other frames in that cluster. Showing them beside the
  keeper turns a search through the camera roll into a single tap — this is the highest-leverage
  screen in the app.
- **Everything is reversible and nothing is destructive.** Overrides are a file; unwinding one is
  an edit. Say so in the UI, because "reject" sounds permanent and is not.

Do not ask for titles, captions or narrative. That is the story step's job, and asking here
doubles the effort for something the model does better.

Two properties to honour rather than smooth over:

- **A reference that matches nothing, or matches two files, is an error, not a no-op.** The app
  writes references it resolved from `trip.json`, so it should never generate an unresolvable
  one — and if the server reports one, that is a bug worth surfacing, not swallowing.
- **Cluster and event ids are never written into overrides.** They are assigned fresh each run.
  An event is addressed by naming a photo inside it, which is exactly what a tap on a photo
  gives you.

---

## Music

**Apple Music tracks are DRM-protected and cannot be exported or mixed.** A picker that shows the
user's subscription library will produce nothing but failures at render time. Sources that work:
Files/iCloud Drive imports, purchased DRM-free downloads, or a licensed catalogue.

The tool ships no audio (see `docs/choosing_music.md`), so this limitation must be legible **at
the picker**, not discovered when the reel comes back silent. The track uploads as an ordinary
hash-addressed asset.

---

## Upload, playback, and the retention window

- **Upload:** hash negotiation against the service, background `URLSession`, per-file retry. The
  client hashes locally with BLAKE2b so the server can answer "which of these do you already
  have?" — dedup and resume fall out of the pipeline's existing identity model.
- **Playback:** `AVPlayer` against a short-lived signed CDN URL. Progressive MP4 streams cleanly
  now that the soft-subtitle mux re-applies `+faststart` (`cf2c75e`).
- **Retention:** media is kept for one month after the most recent photo book or reel, resetting
  on each generation. Two consequences the UI owns:
  - **Offer aspect, music and day-range at render time**, while the media is still there. That is
    cheaper than any re-upload path.
  - **Surface the window closing.** A re-cut after expiry reads 720p proxies and is visibly
    softer — `reel.json` will say so via `clips_enlarged_to_fit_frame`. Better to prompt before.

---

## Milestones

Mirrors the service doc, so the two can be read side by side.

| # | App deliverable | Needs |
| --- | --- | --- |
| **M0** | Scope picker, export, metadata parity proven, hand-off via AirDrop/Files | **No backend.** Shippable alone for anyone with a laptop |
| **M1** | Hashing, background upload, job progress, `trip.json` rendered as a browsable trip | Service M1 |
| **M2** | Reel options, playback from CDN, share sheet | Service M2 |
| **M3** | Curation UI writing overrides, re-run, push notifications | Service M3 |
| **M4** | Story text appears in the book without a manual chat | Service M4 |

M0 is the one that de-risks everything. It needs no server, no account, and no queue, and it
proves the only claim the rest depends on.

---

## Risks

- **Background execution.** `BGProcessingTask` is opportunistic with no guarantee, and jetsam
  kills on memory. Export and upload must be resumable across many short foreground sessions and
  must look intentional while doing it. The pipeline's own resumability does not help here — this
  is the phone's half.
- **Memory during export.** Decoding and re-encoding a large library is where an app gets killed.
  Stream per asset; never hold a batch of full-size images.
- **Live Photos, slow-mo, cinematic mode, RAW+JPEG pairs.** Each has a "which resource do I
  actually export?" answer, and getting one wrong produces a file that looks fine and carries the
  wrong timestamps. Enumerate them explicitly rather than discovering them.
- **The permission prompt.** Full library access is a real ask. The scope picker should make the
  reason obvious before the prompt appears.

---

## Non-goals

- Replacing or degrading the laptop workflow.
- Rendering the reel on device (see the AVFoundation doc).
- Editing photographs. The source library is read-only, in the same spirit as guarantee 1.
- Sharing, comments, public links, multi-user trips.

---

## Open questions

1. ~~**Minimum iOS version**, and whether the app targets SwiftUI only.~~ **Decided 2026-08-09:**
   Swift 6 + SwiftUI, native, minimum iOS 17. Reasoning and module layout in
   [`ios_client_implementation.md`](./ios_client_implementation.md).
2. **Does the app hold a local trip list across devices**, or is the service the only index?
3. **What does the app show while a build runs for an hour?** Progress is real (it can be read
   from `stage_result`), but an hour is an hour.
4. **Does curation re-run happen automatically on edit, or on an explicit "rebuild" tap?** The
   cached-stage story makes automatic feasible; it may still be the wrong default.
5. **Where is the cull threshold?** "Selected 60 of 800 in-range" clearly warrants the nudge and
   "600 of 800" clearly does not. The line between them should be set by watching real
   selections, not guessed here.
6. **Is the alternates strip built from dedup clusters alone, or also from CLIP neighbours?**
   The two solve different problems at different thresholds, and the strip wants "another frame
   of this moment" rather than "something that looks similar".
