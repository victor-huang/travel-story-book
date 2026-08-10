# iOS parity harness — the M0 gate

The entire argument for the iOS architecture is that **the app produces the same shape of file
Photos does**. If that holds, the 1700 existing tests already cover everything downstream. This
directory is the test that decides it, and it is the one check nothing cheaper can replace.

```bash
uv run pytest tests/ios_parity/          # ~40 s: two full pipeline builds
```

Two halves, because a field-level diff and a structural diff catch different failures:

| File | Asks |
| --- | --- |
| `test_field_parity.py` | Does each exported file still carry what the pipeline *reads*? |
| `test_trip_structure_parity.py` | Does the pipeline still *conclude* the same things? |

Both read through the pipeline's own code — `run_exiftool` and `extract_timestamp` — not an
ad-hoc parser. The question is never what `exiftool` prints but what `MetadataStage` concludes,
and those differ everywhere the pipeline resolves rather than reads.

## Every justified difference, and why

A field may differ **only** if it appears here. Anything else fails the diff. Note that none of
these are simply excluded: each is still asserted, against a tolerance or a shape instead of
equality, because an excluded field makes a catastrophic change invisible.

| What differs | Why it is allowed | How it is still checked |
| --- | --- | --- |
| `width`, `height` | The export downscales to a 1080px long edge. This is the point of the export — the service only ever sees 1080px, which is why the report resolves images back to the phone's originals (D4). | `test_the_downscale_actually_happened` asserts the long edge shrank and is ≤ 1920. |
| `duration` | Video only, within one frame. `AVAssetExportSession` re-encodes and the output lands on a frame boundary of the new timescale. | Bounded by the field-parity tolerance; `video.duration_seconds` must still match at the pipeline's precision. |
| `asset_id`, `content_hash`, `bytes` | Ids are prefixes of the BLAKE2b of the file's bytes, and the bytes legitimately differ. | `test_asset_ids_differ_because_the_bytes_do` — if they matched, the exports would be byte-identical copies and the harness would be comparing a set with itself. |
| `thumbnail`, `preview`, `video.poster`, keyframe paths | Derivative paths keyed by content hash. | `test_a_posters_path_differs_only_in_its_hash_directory` proves a poster was still produced, rather than the field going empty. |
| `quality` | Measured **from the pixels**, and the export's pixels are a 1080px resample. A small change is correct. | `test_quality_moves_only_slightly` bounds `overall` to 0.05, and `test_resampling_does_not_sharpen` asserts measured sharpness never *rises* — a resample cannot add detail, so an increase would mean the exporter is applying something it should not. |
| `event_id`, `cluster`, `selected` | Permute under an instant tie — see below. Not an export defect. | `TestKnownPermutation` pins it, and `test_the_same_event_boundaries_exist_on_both_sides` proves the same events exist either way. |

## What this harness found

**`event_id` is not stable when two items share a `taken_utc`.** `tz_before_1.jpg`
(23:10+02:00) and `tz_after_1.jpg` (00:10+03:00) are both 21:10 UTC, and they exchange `#2` and
`#3` between two libraries holding the same logical content: the ordering tie is broken by
something byte-dependent. It is deterministic for identical bytes, so the existing
build-twice-and-diff test cannot see it — it takes a *re-encode* to expose, which is exactly the
iOS case. Filed as a cross-task request in `dev_plan/implementation_tracker.md`; the suggested
fix is to break the tie on filename, which `overrides.toml` already treats as an address.

## What is not covered

- **No comparison against Photos.app's own 1080px export.** The design doc asks for one, and it
  needs a human to export the same assets from Photos on the Mac. What is compared here is
  source against iOS export, which answers "does the export preserve what the pipeline needs"
  but not "does it match Photos byte-for-byte".
- **`iphone_spatial_audio.mov` has no committed export.** Its 1080p export is 15 MB, against
  ~490 KB for the other nine combined, and the properties that make it interesting — an `apac`
  track and six `mebx` streams being dropped — are covered by the Swift suite
  (`ClipExporterTests`). Verified by hand at the time it was written: the pipeline reads the
  export with the same local time, the same `exif_offset` resolution, the same Salzburg place and
  the same `-47.1 dB` mean volume as the source.

## Regenerating the exports

They are produced by the Swift exporters, so this needs macOS and Xcode. There is no committed
driver: build a small `@main` against `ios/Sources/PhotoExport`, call
`FolderWriter.export(_:)` over the fixtures listed in `exported/export_ledger.json`, and copy
the result here — omitting the spatial clip. **Re-run this suite afterwards**: it exists to fail
when an export changes, so a regeneration that quietly alters a field should be caught here
rather than believed.
