# P06 — the export lied about its own content type (2026-07-28)

The second package review validated cleanly on everything mechanical and then found one defect
worth more than all of the first review's ten: the nine exported "videos" were JPEGs.

---

### `kind: "video"`, `export_path: "....mov"`, and a still image on disk

**Category:** correctness
**Cost:** would have broken any consumer that tried to decode the footage, and misled one that
did not

**Symptom.** Nine assets declared `kind: "video"` with paths like
`2026-07-18/media/bee942d96cc81c3c_IMG_1815.mov`. `file` reports each as
`JPEG image data, 1920x1080`.

**Root cause.** Two reasonable decisions, composed into a lie. A video's `preview` in `trip.json` is
its poster frame, because that is the sensible thing for a *report* to show. The package names each
exported file `{asset_id}_{source_filename}` so a human can recognise it. Put together: a JPEG
written under a `.mov` name, described as video.

Nothing in the pipeline was in a position to notice. The file existed, the manifest referenced it,
the reference resolved, the schema validated, and 87 tests passed. Every check was about *presence*,
and the defect was about *identity*.

**Fix.** Two honest modes rather than one dishonest one. The poster ships as
`<asset_id>_poster.jpg` with `export_media_type: "image/jpeg"` and `export_role: "poster_frame"`;
`--video-proxies` transcodes real 720p H.264 MP4s and reports `video/mp4`. A failed transcode falls
back to the poster and records it in `skipped`, rather than leaving a claim of footage that is not
there. A test now asserts no `image/jpeg` export carries a video extension, and another reads the
`ftyp` box out of a generated proxy.

**Lesson.** **A file's name and its declared type are claims, and claims need checking against the
bytes.** Every test I had asked "is the referenced file present?" — the useful question was "is it
what we said it is?". The generalisation for any export: for each declared media type, verify one
file's actual content. `file -b` or a magic-number read is two lines.

There is a second, harder lesson. I verified this package extensively before shipping it — schema
validation, every reference resolved, no archive artifacts, asset ids unique, spot-checks on a dozen
fields — and reported it as verified. All of that was true and none of it touched the defect,
because I checked the *manifest* against the *filesystem* and never the filesystem against reality.
Thorough verification of the wrong property reads exactly like thorough verification.

---

### The package looked more complete than it was, which is worse than looking incomplete

**Category:** design

The reviewer's framing is the part worth keeping: *"the video side currently looks more complete
than it actually is."* A package with no video at all would have been honest and would have got a
storyboard built from stills. A package advertising nine clips gets a storyboard built on the
assumption that someone watched them.

This is the same failure as the `no_speech` / `not_processed` distinction two reviews ago, and as
the privacy block that states whether the home filter ran. Each time, the fix was to say what is
actually there. The pattern is now explicit enough to state as a rule: **an artifact must never
overstate its own contents, and where it cannot provide something, it says so at the point where a
consumer would otherwise assume.**

Hence the prompt change, which is the part that matters most for output quality: without proxies,
five frames sampled across 112 seconds cannot support a confident choice of seconds 43–51. The
prompt now says so and asks for ranges anchored to keyframe offsets, flagged in `uncertainties`.
Asking for exact ranges anyway would have produced confident numbers with nothing behind them.

---

### Marking a bad clip instead of guessing what it is

**Category:** design — a small decision worth recording

The library holds a 0.37-second clip. The reviewer suggested it might be a Live Photo motion
component and proposed `kind: "live_photo_motion"`. I checked: 11 frames, HEVC, audio and data
streams, **no `ContentIdentifier`** — which is the tag that would actually identify a Live Photo.

So it is marked `subtype: "short_clip"` by *duration*, with `storyboard_candidate: false` and one
keyframe instead of five. Not `live_photo`, because I do not know that. Naming it would have been an
inference wearing a fact's clothes, in a field a consumer would reasonably trust.

**Lesson.** When a reviewer offers a plausible cause, check it before adopting their label. The
correct answer was a weaker claim than the one suggested, and the weaker claim is the true one.

---

### One fix broke arithmetic three commits after the last time this happened

**Category:** correctness

Adding offsets to trip bounds meant `min()` over local strings, which is wrong across zones:
`...T09:00+02:00` sorts after `...T08:00+01:00` while being the earlier instant. Fixed by choosing
the bound by `taken_utc` and reporting the corresponding local string.

This is the *second* instance in two days of the same rule — order by UTC, split days by local —
being applied at one site and missed at another. Yesterday it was event durations. Both were caught
by tests, which is the system working, but the pattern is that adding a field to a timestamp
silently changes the semantics of every comparison on it.

**Lesson.** Making a value richer makes its comparisons subtler. When a type gains a dimension,
grep every comparison and sort on it — `min(`, `max(`, `sorted(`, `<` — rather than waiting to be
told which one broke.

---

## Encoded as project rules

- For every declared media type in an export, verify one file's actual bytes. Presence is not
  identity, and "every reference resolves" says nothing about what the references point at.
- An artifact never overstates its contents. Where it cannot supply something a consumer would
  assume, it says so at that point — `video_proxies_included`, `transcript_status`,
  `privacy.home_configured`, `asset_scope`.
- Never request precision the data cannot support. Without proxies, video ranges are estimates
  anchored to keyframe offsets and flagged in `uncertainties`.
- Classify by what is measurable. `short_clip` by duration, not `live_photo` by inference from a
  tag the file does not carry.
- When a value gains a dimension (an offset, a unit, a zone), grep every comparison on it.
