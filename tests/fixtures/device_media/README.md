# Device-captured fixtures

Real captures from an iPhone 17 Pro. **Nothing here is generated, and nothing here can be
regenerated.**

## Why this is not `tests/fixtures/media/`

`tests/fixtures/generate.py` opens with `shutil.rmtree(MEDIA_DIR)` — running it wipes
`tests/fixtures/media/` and rebuilds every file from the deterministic generator. That is
correct for the 27 synthetic fixtures and fatal for these two, because no generator can produce
a spatial-audio track. They live in a directory the generator does not own so that
`uv run python tests/fixtures/generate.py` stays safe to run.

**Do not move these into `media/`.**

## What each file is for, and what it proves

| File | Why it exists |
| --- | --- |
| `iphone_heic_gps_offset.heic` | A *real* HEIC from a current iPhone with GPS **and** `OffsetTimeOriginal`, plus an Apple `ContentIdentifier`. The synthetic `media/heic_gps_offset.heic` covers the same shape; this one covers the real encoder's container. |
| `iphone_spatial_audio.mov` | Spatial audio: an `apac` 4-channel track *alongside* the `mp4a` stereo track, plus **six `mebx` timed-metadata tracks**. Mapping every audio stream is what broke 58 of 69 real clips on the Python side, and no synthetic fixture reproduces it. |

## Coordinates are deliberately not the capture location

Both files were captured at the author's home and this repository is public, so the GPS was
rewritten to Salzburg (47.7949, 13.0475, 540 m) to match the coordinates the synthetic fixture
set already uses. `OffsetTimeOriginal` was moved to `+02:00` at the same time, so the offset
still *agrees* with the GPS: an incoherent pair would silently turn these into
offset-vs-GPS-conflict fixtures, and `media/offset_gps_conflict.jpg` already owns that case.

The video needed more than an `exiftool` rewrite. iPhone clips record per-frame GPS into the
`mebx` track samples, so the original ISO 6709 string survived inside `mdat` after the
container metadata had been rewritten — visible in the raw bytes while every metadata reader
reported the new value. It was patched in place, exploiting the fact that an ISO 6709
coordinate at that precision is a fixed 26 bytes and the replacement
(`+47.7949+013.0475+540.000/`) is the same width, so no atom offset moved. **If you ever
replace these files, grep the raw bytes, not just the tags.**

## What is still missing

No **Live Photo pair**. `iphone_heic_gps_offset.heic` carries a `ContentIdentifier`, so it is
the still half of one, but its paired motion `.mov` was not captured — the two files here are
independent captures nine seconds apart. I13 (`ResourceSelection`) needs a genuine pair, with
matching `ContentIdentifier` on both halves, before Live Photo handling can be tested rather
than assumed.
