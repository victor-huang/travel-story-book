# T50 — the reel, and a font that could not spell München

**Cycle:** Wave 5, T50 (reel v1). Shipped. 1422 tests pass, 0 skips.

## What went right, and why

The reel took roughly a day because **the pipeline had already done the hard part**. Selection,
ordering, chapter structure, previews and `ffmpeg` were all in hand; v1 needed no new Python
dependency. Scoping it as an *export* — a pure function of `trip.json`, like `report.py` and
`package.py` — meant it touched no frozen contract and inherited home exclusion, `reject` and the
content-class filter for free rather than reimplementing privacy.

The one design decision that paid off immediately: **the segment cache key is the segment's own
spec, never its position in the list.** A test asserts that inserting a photo at the front of the
reel reuses every existing segment. It does.

## The bug worth writing down

The first rendered title card read **`A family travel story from July 17□20, 2026`**.

Pillow's `ImageFont.load_default(size=...)` was chosen deliberately — `contact_sheet.py` picked it
so that "legibility does not depend on a TTF being present on the machine", which is a good
instinct. But the bundled font is **ASCII-only in practice**. Probing it:

```
'–' MISSING  '—' MISSING  'é' MISSING  'ü' MISSING
'ö' MISSING  'à' MISSING  'ñ' MISSING  '£' MISSING  '€' MISSING
```

Three things about this are the lesson.

**1. It was already shipping, in a different artifact.** `contact_sheet.py` uses the same font for
cell labels, which carry place names. This trip's geocoder returns English exonyms — "Vienna",
"Munich" — so nothing visibly broke, and the bug sat latent behind a truth that is accidental.
A trip through München or Zürich would have drawn boxes on every contact sheet handed to ChatGPT.
The reel did not introduce this defect; it was the first thing to *look at output* that contained
a character outside ASCII.

**2. A dependency's convenience default is still a design choice.** "Always present" and "renders
the text you have" are different properties, and only the first was checked when the font was
chosen. The fix is `export/fonts.py`: prefer a real system font, and transliterate whatever the
chosen font cannot draw, so the worst case is `Munchen` rather than `M□nchen`. A dropped accent
looks like a limitation; a box looks like a bug.

**3. It exposed a cache the fix could not reach.** The resolved font changes the pixels of a title
card, but nothing about the font was in the segment key — so a user who installed a font later
would keep the transliterated cards forever. This is the same shape as `media.exif_local` landing
with `MetadataStage.version` left at 1. **Ask what happens to a library that already exists.**
`font_identity()` is now part of the key for title segments, and re-running proved it: **5 of 61
segments re-rendered — exactly the five title cards.**

## Verification that earned its keep

**Looking at the output found the bug in seconds.** A 5×5 grid of frames pulled straight out of
the finished MP4 showed the blurred-fill framing working, the day titles landing, and the tofu box.
No assertion had anything to say about any of it. That is now four separate occasions on this
project — flat quality scores, chained clusters, parked vans, and this.

**The motion check was run in both directions.** P06's defect was nine assets declared
`kind: "video"` whose exported bytes were JPEGs, and every presence test passed. So the reel test
measures maximum inter-frame change and I ran it with clip footage and then with the same clips
forced to poster stills: **99.15 versus 0.12**, threshold 5.0. A test of a failure mode has to be
shown to fail, and this one was, with a margin wide enough that the number means something.

## Tests that were wrong rather than code

Three of my own backend tests failed on first run, all test-side:

- `zip(frames, frames[1:], strict=True)` — the lists differ by one by construction. `strict=True`
  is right nearly everywhere and exactly wrong when pairing consecutive elements.
- A hardcoded `["426", "240"]` for 16:9 at height 240. The real answer is **428**: 426.67 rounds to
  427, which is odd, and H.264 with `yuv420p` cannot have an odd axis. I had written the
  even-rounding rule into the code an hour earlier and still hardcoded the un-rounded number into
  the test. The assertion now derives the expected size from `frame_size()`, and a separate test
  pins 428 explicitly with the reason.

## Second pass: clip audio, and two more tests that were wrong

Clip audio and sidechain ducking went in after the first render. Both of the bugs were in the
*measurement*, which is the pattern this file keeps recording.

**`-v error` silenced the thing being measured.** `_loudness` ran `ffmpeg -v error ... volumedetect`
and parsed stderr for `mean_volume`. `volumedetect` reports at *info* level, so the line never
appeared and every call returned the `-999.0` "no reading" sentinel. The assertion was
`before < under`, i.e. `-999.0 < -999.0` — false, so it failed loudly rather than passing. It very
nearly did not: an assertion written `>=`, or any comparison of two sentinels expected to be
equal, would have passed on no evidence at all. **A sentinel that compares cleanly against itself
is a test that can pass while measuring nothing.**

**The tail fade masqueraded as ducking.** The clip fixture was last in `taken_utc` order, so it
landed at the end of the reel — exactly where the music's 2.5 s `afade` lives. The control test
(ducking *disabled*) measured **7.7 dB of "ducking"**. That control is the only reason this was
caught; the positive test passed comfortably and would have shipped a number that was mostly
fade. Fixed by putting the clip between two stills and shrinking the fade for the measurement.
**Write the control, and make it assert a tight bound** — `abs(diff) < 2.0` is what failed at 7.7.

**Tuning was measured, not chosen.** Duck depth across four sidechain settings: 5.0 / 7.1 / 8.4 /
8.8 dB. The default sits at 8.4 where the curve flattens. The measurement bandpasses the music's
own frequency so clip audio cannot be counted as music — measuring the full spectrum would have
shown "louder under clips" and concluded ducking was broken.

**One risk worth having checked.** `_segment_offsets` and `_xfade_chain` independently accumulate
the same crossfade arithmetic. Disagreeing by one crossfade per clip would have put the eighth
clip's sound 4.8 s from its picture — subtly wrong, and invisible in any single-clip test. Audible
stretches came out at 17.7/42.1/65.7/98.9 s against motion at 17.5/42.0/65.5/98.8 s. A unit test
now asserts the two functions agree.

## Third pass: subtitles, and repeating a mistake from the same day

`--subtitles zh,en` landed. Two failures, both mine, both worth recording because one was a
*recurrence*.

**I repeated the `zip` off-by-one I had written up hours earlier.** In the clip-audio pass I hit
`zip(frames, frames[1:], strict=True)` and wrote a retro paragraph about it. In the subtitle pass I
wrote `zip(track.cues, track.cues[1:], strict=True)`. Identical shape, identical failure. Writing a
lesson down did not stop me reaching for the same wrong idiom the next time I needed adjacent
pairs — which says the retro entry was the wrong *kind* of fix. A prose note about a habitual typo
is weak; the enforceable version is a lint rule or a tiny helper (`pairwise`, which is in
`itertools` and would have been correct by construction). **When a mistake is mechanical, encode a
mechanical fix.**

**Rich swallowed the thing I was printing.** `console.print(f"subtitles [{track.language}]: ...")`
rendered as `subtitles : 28 cue(s)` — Rich reads `[zh]` as a style tag and eats it. No error, no
warning, just a missing word in the one line whose whole job was to say which language. Found only
by reading the terminal output rather than the code, which is the same "look at the output" lesson
in yet another medium. Any `[...]` in a `console.print` f-string is a markup risk.

**One decision the environment made for me.** I had planned to visually verify by burning a frame
with ffmpeg's `subtitles` filter. This machine's ffmpeg has **no `subtitles` filter** — no libass.
That is worth knowing beyond the inconvenience: it retroactively confirmed the soft-track design,
because a burn-in feature would have been unbuildable here and unavailable to any user on a stock
Homebrew ffmpeg. Verification instead extracted the embedded `mov_text` stream back out of the MP4
and diffed it against the sidecar — 28 cues in, 28 out, CJK intact. That is the better check
anyway: `mov_text` is exactly where an encoding gets mangled, and a burned-in frame would not have
tested the muxed track at all.

## Fourth pass: burn-in, and the other half of the font bug

The CJK half of the font bug turned out to be worse than the tofu box that started this file.

```
'维也纳的艺术与音乐'  →  ''
```

`renderable()` transliterates what it can and drops what it cannot. Chinese does not decompose to
ASCII, so the whole string was **deleted**. A box says "something is broken"; an empty string says
"there was nothing here". The fix I shipped in the first pass made one failure mode louder and
another one silent, and I did not notice because the only text in play was Latin. **A fallback that
degrades gracefully for one script can fail silently for another — the test set has to include the
scripts the fallback cannot handle.**

The real fix is not a better fallback but a better choice of font: `font_for(text, size)` picks the
first available font that can draw *every* character it is given. Latin text still gets Arial, so
nothing about the existing look changed; Chinese gets Hiragino Sans GB. Title cards use it too, so
a translated day title renders instead of vanishing.

**`crop=w:h:x` silently centres `y`.** The burn-in test compares the bottom of the frame against
the clean reel. My helper built `crop=in_w:in_h/5:in_h*4/5` — three parameters, so ffmpeg read the
third as `x` and centred `y`. Both the "top" and "bottom" bands sampled the *middle* of the frame,
came back identical, and the test failed with `0.0048 > 0.0097`. It failed for the right reason by
luck: the two sentinels were equal, and `>` rejects that. Had the assertion been `>=`, or had I
compared against a constant instead of a control band, it would have passed while measuring a
region with no subtitles in it. **Third time this cycle that a measurement helper, not the code,
was the bug.**

The control band is what makes the burn-in assertion mean anything. Final numbers: bottom-of-frame
difference **6.24 over a title card against 0.00 at the top**, and **0.11 during a clip with no
caption against 0.14 at the top**. Text where a cue is, nothing where none is — and the third
measurement is a control I did not have to construct, because the reel already contains segments
with no caption.

**The environment settled a design question again.** Burn-in composites Pillow-drawn PNGs through
ffmpeg's `overlay` rather than using the `subtitles` filter, because this machine's ffmpeg has no
`subtitles` filter — libass is absent from a stock Homebrew build. The user offered to install a
libass build, and declining was right: the tool must run on what users actually have, and the
Pillow route has no such dependency at all.

## Honest gaps in what shipped

- **No excerpt is the best five seconds of a clip.** `motion_score` is per clip, not per window,
  so all eight excerpts are `fixed_head` — arbitrary, starting at the poster offset, and recorded
  as such in `reel.json`. P05's `highlight_ranges` remains the fix.
- **Cuts are not beat-aligned**, and `reel.json` says `beat_aligned: false` rather than staying
  quiet about it.
- **Criterion 2 is unmet by construction**: a person has to watch it. Every other criterion passing
  says nothing about whether the thing is worth watching.

## Encoded so it cannot recur

- `export/fonts.py` — one funnel for text in images; `contact_sheet.py` now routes every label
  through `renderable()` inside `_truncate_to_width`, the single place all labels pass.
- `tests/unit/test_fonts.py` asserts the bundled-font path specifically, since that is the
  constrained case and the one CI may hit.
- `font_identity()` in the title-card cache key.
- A backend test that adds a photo at the front of the reel and asserts nothing else re-renders.
