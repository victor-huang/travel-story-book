# P01 — profile findings from a real library

Output of `story-book profile` on a real photo/video export, used to replace the guessed
threshold defaults in `config.example.toml` with observed numbers.

**Deliberately anonymized.** Statistics, distributions, and derived config only — no file paths,
no place names, no coordinates, no calendar dates, no home location. Timezones appear only as
*relative* facts ("the trip zone", "9 hours from the trip zone") because an absolute offset pair
narrows down both where someone travelled and where they live. The numbers below are what the
project needs; the rest is not.

The trip's own `config.toml` stays untracked (`.gitignore`), as should yours.

## Library shape

| Metric | Observed |
| --- | --- |
| Total items | 286 |
| Images | 277 (all JPEG) |
| Videos | 9 |
| Total size | 1.9 GB |
| HEIC share | 0% — the export pipeline had already transcoded to JPEG |
| Video duration | 8 minutes total |
| Distinct capture devices | 1 phone (recent iPhone) + 6 items with no device tag |
| Items with no device tag and no GPS | 6 — almost certainly screenshots or saved images |
| Span | 4 days, 4 dates carrying media |
| Items with no usable timestamp | 0 |
| GPS coverage | 98% (6 items missing) |

## Time distribution — the finding that changed a default

Gaps between consecutive items, in minutes, across 285 gaps:

| p50 | p75 | p90 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- |
| 1 | 4 | 15 | 41 | 470 | 703 (11.7 h) |

**Half of all consecutive shots are within one minute of each other.** This library is shot in
dense bursts, which makes the low percentiles useless as an event-boundary signal and makes the
guessed `event_gap_minutes = 90` far too coarse — it would merge genuinely separate stops into
single events.

Event boundaries are *rare* relative to shots-within-an-event (roughly 15–25 boundaries among 285
gaps here), so the boundary threshold sits near **p95**, not p90. A p90 basis over-splits; see
[the retro](../retro/agentic_coding/2026-07-26-p01-real-data.md).

## Timestamp source

| Field used | Items |
| --- | --- |
| `DateTimeOriginal` | 277 |
| `QuickTime:Keys:CreationDate` | 9 |

All 9 videos required `Keys:CreationDate`. Their `CreateDate`, `MediaCreateDate`, and every
`Track*CreateDate` held the **export** time instead — reading the conventional field put all of
them on the day the export was run, inventing a phantom extra day and a 5.9-day gap in a 4-day
trip. This is now a binding amendment to Module 2.

## Offset integrity

| Signal | Value |
| --- | --- |
| Items whose offset matches the trip zone | 273 |
| Items whose offset is 9 hours from the trip zone | 13 |
| Items whose offset **disagrees with their own GPS** | 7 |
| Sustained offset changes (3+ consecutive items) | 2 |
| Naive offset changes (any A→B transition) | 14 |

Read together these are diagnostic; read alone, either misleads. The trip never actually left its
timezone. The 13 outliers are mis-tagged — an edited or re-exported photo can carry the *editing
machine's* offset rather than the camera's — and the "2 sustained changes" are that cluster
entering and leaving. The naive count of 14 was simply wrong.

Consequence: `OffsetTimeOriginal` is a hint, not ground truth, and **GPS must win a
disagreement**. Also a binding amendment to Module 2.

## Derived configuration

What P01 actually produced. Only values with an observational basis were changed.

| Key | Guessed default | Observed value | Basis |
| --- | --- | --- | --- |
| `events.gap_minutes` | 90 | **45** | p95 of inter-item gaps is 41 min |
| `time.suspicious_gap_days` | 3.0 | **2.0** | largest observed gap 0.49 days; tool suggested 1.0, raised to 2.0 because a full trip can have a legitimate photo-free travel day |
| `time.default_timezone` | `UTC` | trip zone | all media in one zone |
| `video.transcribe` | `auto` | `auto` (confirmed) | 9 clips, 8 min; `all` would transcribe silent b-roll |
| `video.whisper_model` | `small` | `small` (confirmed) | no runtime target, so the larger model is free |

**Left untuned on purpose:** `[dedup]`, `[quality]`, `[selection]`, and `events.jump_km`. These
have no observational basis until the P03 truth set exists (~200 labelled photos: event
boundaries, duplicate groups, preferred picks). Tuning them by eye now would bake in guesses
wearing the costume of measurements.

## Caveats

- **One library, one photographer, one device.** These numbers describe a dense, single-city,
  phone-shot trip. A DSLR-and-GoPro road trip would show a different gap distribution and much
  lower GPS coverage. Treat `events.gap_minutes = 45` as tuned for *this* shooting style, not as
  a better universal default — which is why the shipped default in `config.example.toml` is
  unchanged and the profiler suggests per-library instead.
- **HEIC coverage is untested by this library** (0%), since the export had already transcoded.
  Direct-from-phone imports will exercise it, and the fixture set covers it.
- 6 no-device/no-GPS items are unverified as screenshots; T13's content classifier should confirm.

## Reproducing

```bash
uv run story-book profile <folder> --json profile.json
```

Sharing a profile publicly? `--json` output includes the source path and absolute dates. Scrub
those before posting — anonymizing that output is not yet automated.
