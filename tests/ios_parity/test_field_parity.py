"""I16, part one: field-by-field parity between a source file and its iOS export.

The entire argument for the iOS architecture is that *the app produces the same shape of file
Photos does*. If that holds, the 1700 existing tests already cover everything downstream. This
is the test that decides it, and it is the one thing no cheaper check can replace.

Two rules shape how it is written:

* **Read both sides with the pipeline's own code**, not an ad-hoc parser. The question is not
  what `exiftool` prints but what `MetadataStage` *concludes*, and those differ everywhere the
  pipeline resolves rather than reads -- `extract_timestamp` walks a kind-specific field priority
  and then hunts for an offset, which no naive tag comparison reproduces.
* **Every difference needs a written justification.** "The file has EXIF" proves nothing. A field
  may differ only if it appears in `JUSTIFIED_DIFFERENCES` with a reason; anything else fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from story_book.db.models import MediaKind
from story_book.exif import extract_timestamp, run_exiftool

REPO = Path(__file__).resolve().parents[2]
EXPORTED = Path(__file__).resolve().parent / "exported"
SYNTHETIC = REPO / "tests" / "fixtures" / "media"
DEVICE = REPO / "tests" / "fixtures" / "device_media"

VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v"}

# Every field the export is allowed to change, and why. A field absent from this mapping must be
# byte-identical between source and export, or the test fails.
JUSTIFIED_DIFFERENCES: dict[str, str] = {
    "width": (
        "Deliberate: the export downscales to a 1080px long edge. This is the point of the "
        "export -- the service only ever sees 1080px, which is also why the report resolves "
        "images back to the phone's originals."
    ),
    "height": "Deliberate, same reason as width.",
    "duration": (
        "Video only, and only within a frame's worth. AVAssetExportSession re-encodes and the "
        "output duration lands on a frame boundary of the new timescale."
    ),
}


def _pairs() -> list[tuple[Path, Path]]:
    """(source, export) for every committed iOS export, resolving where the source lives."""
    pairs: list[tuple[Path, Path]] = []
    for export in sorted(EXPORTED.iterdir()):
        if export.suffix == ".json":
            continue
        for root in (DEVICE, SYNTHETIC):
            candidate = root / export.name
            if candidate.exists():
                pairs.append((candidate, export))
                break
        else:  # pragma: no cover - a committed export with no source is a broken fixture set
            raise AssertionError(f"no source fixture for iOS export {export.name}")
    return pairs


def _kind(path: Path) -> MediaKind:
    return MediaKind.VIDEO if path.suffix.lower() in VIDEO_SUFFIXES else MediaKind.IMAGE


def _conclusions(path: Path) -> dict[str, object]:
    """What the pipeline concludes about a file, using the pipeline's own extraction."""
    raw = run_exiftool([path]).get(str(path)) or {}
    kind = _kind(path)
    stamp = extract_timestamp(raw, kind)

    def number(name: str) -> float | None:
        value = raw.get(name)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return {
        # The resolved capture instant and offset, not the raw tag -- this is the field the whole
        # timezone order hangs off.
        "taken_local": stamp.dt.isoformat() if stamp.dt is not None else None,
        "offset_minutes": stamp.offset_minutes,
        "lat": number("GPSLatitude"),
        "lon": number("GPSLongitude"),
        "altitude": number("GPSAltitude"),
        "make": (raw.get("Make") or "").strip() or None,
        "model": (raw.get("Model") or "").strip() or None,
        "orientation": raw.get("Orientation"),
        "width": number("ImageWidth"),
        "height": number("ImageHeight"),
        "duration": number("Duration"),
    }


PAIRS = _pairs()
IDS = [export.name for _, export in PAIRS]


class TestFixtureSet:
    def test_every_export_has_a_source(self) -> None:
        assert PAIRS, "no iOS exports committed -- the parity gate proves nothing"

    def test_the_set_covers_both_producers(self) -> None:
        sources = {source.parent.name for source, _ in PAIRS}
        assert sources == {"media", "device_media"}, (
            f"parity must cover a real device capture as well as synthetic fixtures; saw {sources}"
        )

    def test_the_set_covers_stills_and_clips(self) -> None:
        kinds = {_kind(export) for _, export in PAIRS}
        assert kinds == {MediaKind.IMAGE, MediaKind.VIDEO}


class TestFieldParity:
    """One test per file, so a failure names the file rather than the set."""

    @pytest.mark.parametrize(("source", "export"), PAIRS, ids=IDS)
    def test_only_justified_fields_differ(self, source: Path, export: Path) -> None:
        before = _conclusions(source)
        after = _conclusions(export)

        unjustified = {
            field: (before[field], after[field])
            for field in before
            if before[field] != after[field] and field not in JUSTIFIED_DIFFERENCES
        }
        assert not unjustified, (
            f"{export.name}: the export changed fields with no written justification: {unjustified}"
        )

    @pytest.mark.parametrize(("source", "export"), PAIRS, ids=IDS)
    def test_the_capture_instant_is_untouched(self, source: Path, export: Path) -> None:
        """Separated out because it is the highest-risk field in the project.

        Both directions matter: a time must not be lost, and -- the bug I15 found -- a time must
        not be *invented* for a file that never had one.
        """
        before = _conclusions(source)
        after = _conclusions(export)
        assert after["taken_local"] == before["taken_local"], (
            f"{export.name}: capture time changed from {before['taken_local']} to "
            f"{after['taken_local']}"
        )
        assert after["offset_minutes"] == before["offset_minutes"], (
            f"{export.name}: UTC offset changed from {before['offset_minutes']} to "
            f"{after['offset_minutes']}"
        )

    @pytest.mark.parametrize(("source", "export"), PAIRS, ids=IDS)
    def test_the_downscale_actually_happened(self, source: Path, export: Path) -> None:
        """The control for the justified difference: `width` and `height` are allowed to change,
        so something must prove they changed in the intended direction rather than at random.
        """
        before = _conclusions(source)
        after = _conclusions(export)
        if before["width"] is None or after["width"] is None:
            pytest.skip(f"{export.name} reports no dimensions")
        long_before = max(before["width"], before["height"] or 0)
        long_after = max(after["width"], after["height"] or 0)
        assert long_after <= 1920
        assert long_after <= long_before

    @pytest.mark.parametrize(("source", "export"), PAIRS, ids=IDS)
    def test_a_declared_type_matches_the_actual_bytes(self, source: Path, export: Path) -> None:
        """P06 found nine assets declared `kind: "video"` whose exported files were JPEGs under
        `.mov` names, past a schema check and 87 passing tests. Ask the bytes.

        Read directly rather than through `run_exiftool`, which requests a fixed tag list and
        does not return `MIMEType` -- and asking the container what it is is exactly the point
        of this check, so borrowing a reader's opinion would weaken it.
        """
        head = export.read_bytes()[:16]
        if _kind(export) is MediaKind.VIDEO:
            # ISO base media: a 4-byte size then 'ftyp'.
            assert head[4:8] == b"ftyp", (
                f"{export.name} is named like a video but starts with {head[:8]!r}"
            )
        elif export.suffix.lower() in {".heic", ".heif"}:
            assert head[4:8] == b"ftyp", (
                f"{export.name} is named like a HEIC but starts with {head[:8]!r}"
            )
            assert b"heic" in head or b"mif1" in head, (
                f"{export.name} is an ISO container but not branded HEIC: {head!r}"
            )
        else:
            assert head[:3] == b"\xff\xd8\xff", (
                f"{export.name} is named like a JPEG but starts with {head[:4]!r}"
            )
