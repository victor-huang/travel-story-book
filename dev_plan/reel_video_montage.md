# Reel — a Memories-style video montage from `trip.json`

> Feature plan, created 2026-08-02. Companion to
> [`mvp_process_from_picture_to_stories.md`](./mvp_process_from_picture_to_stories.md) (design)
> and [`implementation_tracker.md`](./implementation_tracker.md) (progress, Wave 5).
> This doc is the **what and why** for the reel specifically. Amend it rather than letting code
> diverge.

Turn a finished trip into a short watchable video — the thing Apple Photos calls a Memory:
stills in time order, a few seconds each, gentle motion, title cards, music, and the trip's
video clips cut in.

---

## Why this is cheap, and where the cost actually is

The expensive part of a montage is not rendering. It is deciding **which frames, in what
order, grouped how** — and this pipeline has spent all of Phase 1 answering exactly that.
Measured on the real Vienna/Munich trip (274 assets in `trip.json`):

| The renderer needs | Already in `trip.json` |
| --- | --- |
| Which photos are worth showing | 47 day highlights, 9 trip highlights, 64 selected in total — human-corrected via `overrides.toml` |
| Order | `taken_utc` on every asset; the standing rule is already "order by UTC, split days by local" |
| Chapter structure | 4 days → 20 events → stops, with labels and places |
| Words for title cards | `story.json`: 13 chapter titles, per-day narrative |
| Full-resolution stills | `previews/` at 1600 px long edge, `thumbs/` at 480 px |
| A route to animate | per-day `path`, already Douglas-Peucker simplified |
| Footage | 9 clips, 478 s total (8.0 min); 8 of them over 10 s |
| Geometry for framing | `geometry.orientation` and `aspect_ratio` per asset |
| A renderer | `ffmpeg`, already a required system binary |

**No new Python dependency is required for the first version.** ffmpeg alone covers concat,
crossfade (`xfade`), blurred-fill pillarboxing, Ken Burns (`zoompan`), and audio mixing.

The cost is concentrated in three places, and all three are honest constraints rather than
work items we can schedule away.

---

## The three hard parts

### 1. Music is a licensing problem, not a technical one

Apple's Memories work because cuts land on beats and the track swells. We cannot ship music:
anything bundled would make a public repo undistributable, and there is no free-and-clear
catalogue that is also good. So the track is **user-supplied or absent**:

```
story-book reel --out <dir> --music ~/Music/something.m4a
```

With no `--music`, the reel is silent, or plays the clips' own audio. Both are fine; neither
is magic. Beat-synchronised cutting needs onset detection (`librosa` or `aubio`, one new
optional extra) and is deferred to v2 — fixed cadence is watchable and costs nothing.

**Consequence for the artifact:** the reel states in `reel.json` whether music was supplied and
whether cuts were beat-aligned. The rule that an artifact never overstates its contents
(`video_proxies_included`, `transcript_status`, `privacy.home_configured`) applies here too.

### 2. We cannot pick the good five seconds of a 112-second clip

`video.motion_score` is **one number for the whole clip**. Clip durations on the real trip run
0.4 s to 111.9 s, with six over 30 s. Excerpting on a whole-clip score is arbitrary, and
labelling the result "the highlight" would be inventing a measurement the pipeline never
computed — this project's single most repeated failure mode, and the reason the ChatGPT package
reports `precision: "city"` instead of a fabricated confidence.

This is already logged as **P05 → Phase 2: automatic `highlight_ranges`** (per-window motion and
stability scoring with a reason string). Until it exists, the honest options are:

1. a fixed excerpt (first *N* seconds after the poster offset), labelled as arbitrary;
2. a range named by `story.json`, since the model *has* seen keyframes — the `video_scenes`
   section of the story schema already exists for this and is currently unused;
3. the poster frame as a still, treating the clip as a photograph.

**v1 does 2 where the story supplies a range, and 1 otherwise, and records which in `reel.json`.**

### 3. The library is 54% landscape / 46% portrait

149 landscape against 125 portrait. In a 16:9 frame nearly half the photos must be pillarboxed
or cropped hard; in 9:16 the problem simply inverts. Apple crops toward faces. We store
`face_count` and `face_max_frac` but **no bounding boxes** — schema has the two scalars only —
so saliency-aware cropping is new analysis, not a rendering choice.

v1 uses **blurred-fill pillarbox**: the frame scaled to cover and heavily blurred as a
background, the photo scaled to fit on top. It is the standard answer, it never crops anything
away, and it looks deliberate rather than broken.

---

## Where this sits in the roadmap

The plan doc puts "narrated or cinematic video rendering" **out of scope for Phase 1** and
"narrated video assembly, cinematic edits" in **Phase 3**. That remains right, and this feature
does not overturn it.

What is pulled forward is a deliberately narrower thing:

| Phase 3 as written | The reel |
| --- | --- |
| Narration | None. Title cards only. |
| Cinematic editing | Fixed or beat cadence, one transition. |
| New analysis of the media | **None.** Renders what `trip.json` already decided. |

That last row is the whole justification. The reel is an **export**, in the same class as the
HTML report and the ChatGPT package: a pure function of `trip.json` plus derived images. It
adds no stage to the pipeline, reads no new signal out of the photographs, and can therefore
be built and thrown away without touching anything frozen.

---

## Design

### It is an export, not a pipeline stage

`src/story_book/export/reel.py`, alongside `report.py` and `package.py`, driven by a new
`story-book reel` command. Those two are also *listed* in the pipeline order but implemented as
exports, and the reel follows them exactly.

**It renders from `trip.json` and never reads the DB.** Same reason the report and package
don't: `trip.json` is the canonical artifact, and nothing derived from insertion order may
leave the database. It also means the reel inherits the home-exclusion filter and the
`reject`/content-class rules for free, rather than reimplementing privacy.

### Output layout

```
<out>/reel/
  trip.mp4              the reel
  reel.json             what was rendered and what was assumed
  .cache/segments/      one intermediate .mp4 per segment, keyed by spec hash
```

`<out>/reel/` is derived and disposable like everything else under `--out` except
`<out>/story/`. `reel` wipes only its own directory.

### Resumability

The non-negotiable applies unchanged: rendering ~60 segments of a 3-minute reel is minutes of
ffmpeg, and an interrupt must not cost all of it. Each segment renders to
`.cache/segments/<hash>.mp4` where the hash covers **the full segment spec** — asset id,
duration, transition, framing, filter chain, and a `reel_version` string — then the final pass
is a concat plus the audio mix. Bumping `reel_version` invalidates everything, exactly like a
stage `version`.

The key must be a function of the spec, never of position in the list, or inserting one photo
at the front invalidates every segment behind it.

### Title cards without a font dependency

`drawtext` needs an ffmpeg built with libfreetype and a font path that exists on the user's
machine — two things we cannot assume. **Render title cards as PNGs with Pillow** (already a
dependency, already used by the contact sheets) and feed them in as stills. Same code path as
a photograph, no new binary requirement, and the typography matches the report.

### The video-pixels problem

The reel needs moving footage; `trip.json` publishes only posters and keyframes, under
`.cache/video/`. It deliberately does **not** publish source paths — a published artifact must
not leak the source tree. Three ways to get pixels, in preference order:

1. **Reuse the H.264 proxies** that `package --video-proxies` already produces (720p, CRF 28).
   Good enough for a 1080p reel, costs nothing new, and already exists.
2. **`--source <dir>`**, opened read-only, resolving `filename` against it. Best quality;
   requires the user to still have the tree.
3. **Poster and keyframes as stills.** Always available. The clip becomes a photograph, and
   `reel.json` says `video_rendered_as: "stills"`.

v1 implements all three with that fallback order, and records which was used.

---

## Versions

### v1 — the watchable one — **shipped 2026-08-02 (T50)**

Measured on the real trip: **61 segments → 2m41s at 1920×1080**, cold render **78 s**, re-render
from cache **28 s**. 5 title cards, 48 stills, 8 clips, all 8 with real proxy footage. No new
Python dependency, as planned.

- 1080p, 30 fps, H.264 + AAC, `-movflags +faststart`.
- Day highlights in `taken_utc` order, ~3 s each, `xfade` crossfade.
- Blurred-fill pillarbox for off-aspect stills.
- A title card per day from `story.json`'s chapter title, or the date and place if no story.
- Video clips per the fallback above.
- Optional `--music`, mixed under, with a fade at the tail.
- `reel.json` recording every assumption.
- `--day 2026-07-18` to render one day; `--assets` to render an explicit list.

Expected length on the real trip: 47 highlights + 8 clips ≈ **2.5–3 minutes**.

### Subtitles — shipped 2026-08-02 (T54, T55)

Selectable tracks from `story.json`'s `translations`, plus an optional burned-in copy. Soft by
default for three reasons that all turned out to matter: a viewer can switch or disable them,
adding a language is a re-mux rather than a re-render, and the player supplies the font. Burn-in
composites Pillow-drawn PNGs rather than using ffmpeg's `subtitles` filter, because **the ffmpeg
this project was developed against has no such filter** — libass is not in a stock Homebrew build,
so a libass-based design would have been unbuildable here and unavailable to most users.

### v2 — the one that feels intentional

- Beat-synced cuts (`librosa` or `aubio`, optional extra `--extra audio`).
- Ken Burns via `zoompan`. **Known issue:** `zoompan` quantises zoom per output frame at input
  resolution and visibly jitters; the usual mitigation is to upscale before the filter and scale
  back down after. Verify on real frames before committing to it — this is exactly the kind of
  thing that looks fine in a test and bad on a screen.
- An animated map segment per day from the day `path`, reusing the report's Leaflet-free SVG
  renderer as frames.
- Pacing that responds to the day: more time on trip highlights than on event samples.

### v3 — Phase 2/3 territory

Per-window video scoring (P05's `highlight_ranges`), saliency and face-aware cropping, music
that drives structure rather than just cadence. Weeks, and gated on analysis that does not
exist yet.

---

## Decisions taken (2026-08-02)

1. **Aspect ratio: 16:9, configurable.** `reel.aspect` accepts any `"W:H"`; `--aspect` overrides
   per run. 9:16 works and is tested. Note the request said "the phone's 16:9", which points two
   ways — a phone held upright is 9:16 — so the ratio is a free-form setting and the default is
   the literal 16:9. Switching is one flag, not a rewrite.
2. **Clip audio: on, with the music ducked under it.** Reversed on 2026-08-02 at the traveller's
   request, and it was the right call — a street performer or an orchestra is usually *why* the
   clip is in the reel, and an unrelated bed of music over the top is the one thing that reliably
   ruins it. Each clip's own audio is delayed onto the timeline; where music is also present the
   clip bus drives a `sidechaincompress` so the music recedes and recovers with the clip rather
   than at boundaries computed from nominal segment lengths. `--no-clip-audio` turns it off.
3. **Seconds per still: 3.0**, `reel.seconds_per_still`.
4. **Music: user-supplied, always.** `--music <file>`, any format ffmpeg reads, looped to length
   with a tail fade. Nothing ships with the tool. A track chosen to feel out the product is a
   local matter; publishing the render is not, and `reel.json` records what was mixed in.
   How to choose one is documented for users in
   [`docs/choosing_music.md`](../docs/choosing_music.md) — length against reel duration, steady
   tempo (which T51 will need), flat dynamics because of the duck, and mood taken from
   `story.json` rather than from the destination.

All are fields on `ReelConfig`, per the standing rule that every threshold lives in `config.py`.

---

## Success criteria

Measured on the real trip, not a fixture.

1. `story-book reel --out <dir>` produces a playable `trip.mp4` with no `--source` and no
   `--music`.
2. **Watch it.** This project has three times found in seconds what the eval harness could not
   find at all — flat quality scores, chained clusters, a selection full of parked vans. A reel
   that satisfies every assertion and is unwatchable is the expected failure mode here, so the
   criterion is a person watching the whole thing.
3. Interrupting the render and re-running recomputes only unfinished segments, proven by a real
   `SIGINT` and a segment count — not by an exit code alone (T43 was told a false pass three
   times that way).
4. Every declared media type is verified by **`file -b` on the actual bytes**, and one clip is
   confirmed to contain motion rather than a repeated still. P06 found nine assets declared
   `kind: "video"` whose exported files were JPEGs under `.mov` names; presence tests all passed.
5. `reel.json` states music, beat-alignment, video source, and excerpt provenance, and each
   claim is checked against what actually happened.
6. Two renders of an unchanged library produce byte-identical segment hashes.
7. Source tree provably unmodified, including under `--source` (extend
   `tests/backend/test_non_destructive.py`).

## Result against the criteria (2026-08-02) — **7 pass · 0 open**

| # | Criterion | Verdict |
| --- | --- | --- |
| 1 | Playable `trip.mp4` with no flags | **PASS** — 61 segments, 2m41s |
| 2 | A human watches the whole thing | **PASS** — the traveller watched it and called it worth watching (2026-08-02) |
| 3 | Interrupt recomputes only unfinished segments | **PASS** — measured, see below |
| 4 | Declared media verified by actual bytes | **PASS** — `ftyp` magic, and motion measured |
| 5 | `reel.json` claims check out | **PASS** |
| 6 | Two renders agree on every segment hash | **PASS** |
| 7 | Source tree unmodified | **PASS** |

**Criterion 2 is the only one that mattered, and it is the only one no test could reach.** Every
other criterion checks that the reel is *correct*; this one asks whether it is any good. It passed
on the version with Crinoline Dreams, clip audio ducked underneath, and 1.4x Chinese subtitles
burned in — after four rounds of the traveller looking at output and saying what was wrong. Worth
recording that the sequence was always render → look → correct, never render → assert → ship.

**Criterion 3 got an unplanned live demonstration.** Adding the font to the title-card cache key
mid-build re-rendered **5 of 61** segments — exactly the five title cards — and reused the 48
stills and 8 clips untouched. That is the invalidation being precise rather than merely present.

**Criterion 4** is the one worth stating with its margin, because P06's defect passed every
presence test. Maximum inter-frame change over the rendered reel: **99.15** with clip footage
versus **0.12** when the same clips fall back to poster stills. The assertion sits at 5.0, so it
is not distinguishing noise — it was run both ways and shown to fail the second.

### Audio, measured (added 2026-08-02)

Sound lands where the picture does. Audible stretches in the finished reel begin at **17.7,
42.1, 65.7, 98.9 s** against motion beginning at **17.5, 42.0, 65.5, 98.8 s** — within the 0.4 s
resolution of the measurement, with no drift accumulating across eight clips. That was the risk
worth checking: `_segment_offsets` and `_xfade_chain` are two accumulations of the same
crossfade arithmetic, and a discrepancy of one crossfade per clip would put the last clip's
sound 4.8 s from its picture. A unit test now pins the two together.

Duck depth was tuned by measurement, isolating the music in its own frequency band with a
bandpass so clip audio could not be counted as music:

| threshold | ratio | duck |
| --- | --- | --- |
| 0.02 | 8 | 5.0 dB |
| 0.01 | 12 | 7.1 dB |
| **0.005** | **20** | **8.4 dB** |
| 0.003 | 20 | 8.8 dB |

**0.005 / 20** is the default: clearly audible, and the curve is flat past it. Verified on the
real trip at **−28.8 dB between clips against −37.1 dB under them**.

## What this will not fake

- No "best moment" of a clip until per-window scoring exists.
- No aesthetic or emotional ranking. Selection is technical quality plus human overrides, and
  the reel inherits it unchanged.
- No claim of beat-sync when cadence is fixed.
- No music in the repository.
- No claim that a clip has sound when its source has no audio track. `reel.json` names those
  clips under `clips_with_no_audio_track`.
