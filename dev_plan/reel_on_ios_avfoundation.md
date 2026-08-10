# Reel on iOS — rendering the montage with AVFoundation instead of ffmpeg

> **Status: draft, optional, not scheduled.** Nothing here is claimed in
> [`implementation_tracker.md`](./implementation_tracker.md), and no code depends on it.
> Created 2026-08-09.
>
> Companion to [`reel_video_montage.md`](./reel_video_montage.md), which is the **what and why**
> for the reel itself and remains the source of truth, and to
> [`ios_backend_service.md`](./ios_backend_service.md), the hosted path this would eventually
> replace. This doc covers one question only: what it would cost to render the same reel with
> Apple's frameworks, on device, with no ffmpeg.

The Mac workflow is unchanged by anything in this document. An iOS renderer is an **addition**,
never a replacement — `story-book reel` on a laptop stays the reference implementation.

---

## When this becomes worth doing

Not yet. Under the current iOS plan the phone exports 1080px stills and 1080p clips, uploads
them, and a Python backend runs the existing pipeline and renders the reel with ffmpeg. That
works, and an AVFoundation renderer buys nothing it needs.

Three triggers would change that, in rough order of likelihood:

| Trigger | Why ffmpeg-on-a-server stops being enough |
| --- | --- |
| **Self-contained app** | The reel is the last server dependency besides the LLM call. Killing it makes the app work with no backend at all, which also restores the offline half of guarantee 3. |
| **Instant re-render** | Curate, tap, watch. A round trip to a queue cannot feel like that; VideoToolbox on device can. |
| **Upload cost stops being acceptable** | ~600 MB per trip is fine on wifi and not fine on cellular or a long trip. |

Until one of those is real, this is a document and not a task.

---

## The enabling fact

`build_plan` in `export/reel.py` is already pure — its docstring says so: *"Decide what the reel
contains. Pure: no filesystem, no ffmpeg, no clock."* `Segment` carries everything the pixels
depend on (`asset_id`, `clip_start`, `seconds`, `source_role`, `with_audio`), by design, because
the whole struct feeds the segment cache key.

So the file splits cleanly:

| Half | Roughly | Fate |
| --- | --- | --- |
| Planning — `build_plan`, `ReelPlan`, `Segment`, `segment_key`, story ranges, `_segment_offsets` | ~500 lines | **Portable logic.** Not part of the rewrite |
| Rendering — filter graphs, ffmpeg invocation, card drawing, muxing | ~600–700 lines | Rewritten |

**Prerequisite, and the one thing worth doing now regardless: publish the plan as JSON.** Have
`story-book reel` write its `ReelPlan` alongside `reel.json`. It costs an afternoon, it is useful
on its own for debugging what the reel decided, and it becomes the contract two renderers share.
Without it, a Swift renderer would have to re-derive planning logic and the two would drift —
which is the failure this project has a standing rule against.

---

## Piece by piece

| `reel.py` does | AVFoundation equivalent | Difficulty |
| --- | --- | --- |
| Crossfade (`xfade=transition=fade`) | Two video tracks, `AVMutableVideoCompositionLayerInstruction.setOpacityRamp` | **Easier.** The most standard operation in the framework |
| `_fill_filter` — scale-to-fit over a blurred, darkened cover copy | `CIGaussianBlur` + `CIColorControls` + composite, via `AVVideoComposition(asset:applyingCIFiltersWithHandler:)` | Comparable |
| Title/end cards — Pillow + `export/fonts.py` (171 lines) | CoreText into a `CALayer` | **Easier, and strictly better.** See below |
| End-card mosaic | Core Graphics tiling | Easy |
| Clip audio delayed to its timeline position, per-bus volumes, music fade-out | `AVMutableAudioMix` + `AVMutableAudioMixInputParameters` volume ramps; placement is inherent in the composition | Easy |
| Encoding (`libx264`, preset + CRF) | VideoToolbox via `AVAssetWriter` | Fast — but see *Encoder honesty* |
| Stills held for N seconds (`-loop 1`) | No equivalent. `AVAssetWriter` writing frames, or a compositor that ignores its source and draws the image | **Harder.** ffmpeg gives this away free |
| Selectable subtitle tracks (`export/subtitles.py`, 496 lines) | tx3g timed text via `AVAssetWriter` — supported, poorly documented; `AVAssetExportSession` will not help | **Hard.** Cut from a v1 |
| `sidechaincompress` — music ducked by the clips' actual audio | **None. There is no sidechain in `AVAudioMix`, and `AUDynamicsProcessor` exposes no key input** | **The wildcard.** See below |

### What gets better

Two things, and one of them is a bug class rather than a feature.

**Fonts.** `export/fonts.py` exists because Pillow's `load_default` has no `é ü ö à ñ – —` and a
first fix transliterated instead, silently deleting every CJK character
(`retro/agentic_coding/2026-08-02-t50-reel-and-the-tofu-box.md`). CoreText does script-aware font
fallback natively. The 171 lines mostly disappear, and so does the failure mode.

**Hardware encode.** VideoToolbox on an A-series chip will likely beat a rented CPU running
x264, which is the thing that makes tap-to-watch plausible at all.

### Encoder honesty

VideoToolbox has no CRF. Quality is a bitrate or quality-target, so `config.reel.x264_preset`
and `x264_crf` do not map and `reel.json` must not pretend they do — it reports what was actually
used, or the fields are absent. Standing rule: an artifact never overstates its contents.

---

## The ducking problem

This is the only item with no equivalent, and it should be decided before any code is written.

Today, where a clip has its own audio, the music is ducked by a real sidechain compressor keyed
on the clip bus — *"driven by the actual sound rather than by nominal segment boundaries"*
(`_audio_graph`). Nothing in AVFoundation does this.

The workable substitute: the timeline positions are already known exactly (`_segment_offsets`),
so measure each clip's loudness envelope offline and generate music volume ramps that approximate
the duck. It is deterministic and arguably more controllable than a compressor.

But it is a **behaviour change, not a port**, and it must be labelled as one:
`ducking: "envelope_ramps"` in `reel.json`, never silently implying the same processing. Budget
about a week on its own, most of it spent making it not sound worse.

---

## Design, if it is built

Follows the existing reel design rather than inventing a second one.

- **The plan is the contract.** Swift consumes the published `ReelPlan` JSON and renders it. It
  does not re-derive planning, does not read `trip.json` structure beyond what the plan names,
  and never reads a DB.
- **Segment cache, unchanged in spirit.** One intermediate per segment keyed by the spec hash,
  never by position. A `reel_version` equivalent invalidates everything. An interrupted render
  loses one segment.
- **`reel.json` stays the honest record**, with renderer-specific fields: which renderer produced
  it, the encoder settings actually used, the ducking method, and whether subtitles were soft
  tracks or burned in.

---

## Acceptance criteria

The point is not "it produces a video." It is "it produces *this* video."

1. **Same plan, both renderers, diffed.** Feed one published `ReelPlan` to ffmpeg and to
   AVFoundation. Total duration and every segment boundary match within one frame.
2. **Frames compared, not just counted.** Sample frames at fixed offsets and look at them side by
   side. This is the only check that catches a compositor quietly centring a crop or dropping a
   frame — the same class of defect as `crop=w:h:x` silently centring `y`.
3. **Audio measured, with a control.** Assert the music level under a clip is lower than between
   clips, by a stated margin. A measurement that can come back empty must be shown not to have.
4. **The scripts the fallback cannot handle.** A CJK day title and an accented place name render
   as glyphs, verified by looking at the frame. The Python renderer shipped a `July 17□20` title
   card; a second renderer gets the same test.
5. **Real output, watched.** Not a test suite result. Watch the reel.

---

## What this will not fake

- It will not claim beat alignment, sidechain ducking, or a CRF it did not use.
- It will not become a second source of planning truth. If the two renderers disagree about
  *what* the reel contains, that is a bug in one of them, not a variant.
- It will not justify degrading the Mac path. `story-book reel` stays the reference.

---

## Estimate

| Item | Rough |
| --- | --- |
| Video path — composition, crossfades, fill filter, cards, stills-as-video | 3–4 weeks |
| Audio — buses, ramps, fade | Included above |
| Ducking by envelope ramps | +1 week |
| Subtitles as soft tracks | Deferred; burn-in only in v1 |
| Conformance harness (criteria 1–3) | +3–4 days |

Assumes fluency with `AVVideoCompositing` and `AVAssetWriter`. Learning those on the job is a
notoriously fiddly corner of the framework — double it.

---

## Open questions

1. Does the plan JSON get published now, as a standalone improvement, or only if this is built?
   (Recommendation: now. It is cheap and independently useful.)
2. Burn-in or drop subtitles entirely in a v1?
3. Does the iOS reel need to be byte-comparable, or only perceptually equivalent? Criterion 1
   assumes the latter — hardware and software encoders will never produce identical bytes.
