"""Phase 0 profiler: what is actually in a trip folder, and what the thresholds should be.

Deliberately standalone -- it touches no database and runs before the pipeline exists. Its
purpose is to replace the guessed defaults in `config.example.toml` with numbers observed from
real media, so every downstream stage is tuned against reality rather than intuition.

It reads EXIF itself rather than depending on the metadata stage (T11), because a diagnostic you
must build a database to run is a diagnostic nobody runs. The read here is intentionally
shallow: T11 owns real metadata extraction, timezone resolution, and persistence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from story_book.db.models import MediaKind
from story_book.media_types import IGNORED_NAMES, classify, is_hidden

EXIFTOOL_FIELDS = [
    "-SourceFile",
    "-MIMEType",
    "-Make",
    "-Model",
    "-DateTimeOriginal",
    "-OffsetTimeOriginal",
    "-CreateDate",
    "-MediaCreateDate",
    "-GPSLatitude",
    "-GPSLongitude",
    "-Duration",
    "-ImageWidth",
    "-ImageHeight",
]
EXIFTOOL_CHUNK = 500


@dataclass(slots=True)
class Item:
    """One scanned file with the shallow metadata the profiler needs."""

    path: Path
    kind: MediaKind
    bytes: int
    device: str | None = None
    taken: datetime | None = None
    utc_offset_minutes: int | None = None
    has_gps: bool = False
    duration: float | None = None

    @property
    def extension(self) -> str:
        return self.path.suffix.lower()


@dataclass(slots=True)
class GapStats:
    """Distribution of time gaps between consecutive photos, in minutes."""

    count: int = 0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    largest: float = 0.0


@dataclass(slots=True)
class Profile:
    source: Path
    images: int = 0
    videos: int = 0
    total_bytes: int = 0
    ignored_files: int = 0
    extensions: Counter[str] = field(default_factory=Counter)
    devices: Counter[str] = field(default_factory=Counter)
    device_gps: dict[str, int] = field(default_factory=dict)
    without_timestamp: int = 0
    without_gps: int = 0
    offsets: Counter[str] = field(default_factory=Counter)
    timezone_crossings: int = 0
    first: datetime | None = None
    last: datetime | None = None
    local_dates: list[str] = field(default_factory=list)
    largest_day_gap_days: float = 0.0
    late_night_items: int = 0
    video_seconds: float = 0.0
    gaps: GapStats = field(default_factory=GapStats)
    exiftool_available: bool = True

    @property
    def total(self) -> int:
        return self.images + self.videos

    @property
    def span_days(self) -> int:
        if self.first is None or self.last is None:
            return 0
        return (self.last.date() - self.first.date()).days + 1

    @property
    def gps_coverage(self) -> float:
        if not self.total:
            return 0.0
        return 1.0 - (self.without_gps / self.total)

    @property
    def heic_share(self) -> float:
        if not self.images:
            return 0.0
        heic = self.extensions[".heic"] + self.extensions[".heif"]
        return heic / self.images


def scan(source: Path) -> tuple[list[Path], int]:
    """Every importable media path, plus a count of everything skipped."""
    media: list[Path] = []
    ignored = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if is_hidden(relative) or path.name in IGNORED_NAMES:
            ignored += 1
            continue
        if classify(path) is None:
            ignored += 1
            continue
        media.append(path)
    return media, ignored


def read_metadata(paths: list[Path]) -> dict[str, dict]:
    """Batch EXIF read. One exiftool process per chunk, never one per file.

    Per-file spawn is roughly a 20x slowdown on a large library; the argument list is fed over
    stdin so a folder of 8,000 files cannot blow past ARG_MAX.
    """
    if not paths or not shutil.which("exiftool"):
        return {}

    results: dict[str, dict] = {}
    for start in range(0, len(paths), EXIFTOOL_CHUNK):
        chunk = paths[start : start + EXIFTOOL_CHUNK]
        # No -fast2: it skips the moov atom, which silently zeroes video Duration.
        completed = subprocess.run(
            ["exiftool", "-json", "-n", *EXIFTOOL_FIELDS, "-@", "-"],
            input="\n".join(str(p) for p in chunk),
            capture_output=True,
            text=True,
        )
        if not completed.stdout.strip():
            continue
        for entry in json.loads(completed.stdout):
            results[entry["SourceFile"]] = entry
    return results


def _parse_exif_datetime(value: object) -> datetime | None:
    """EXIF stamps look like '2026:07:18 09:20:00', sometimes with a trailing offset."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith(("0000", "    ")):
        return None
    text = text.split("+")[0].split("Z")[0].strip()
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], pattern)
        except ValueError:
            continue
    return None


def _parse_offset(value: object) -> int | None:
    """'+02:00' -> 120 minutes."""
    if not isinstance(value, str) or len(value) < 6 or value[0] not in "+-":
        return None
    try:
        hours, minutes = int(value[1:3]), int(value[4:6])
    except ValueError:
        return None
    total = hours * 60 + minutes
    return -total if value[0] == "-" else total


def build_item(path: Path, meta: dict, size: int) -> Item:
    kind = classify(path) or MediaKind.IMAGE
    make = (meta.get("Make") or "").strip()
    model = (meta.get("Model") or "").strip()
    device = " ".join(part for part in (make, model) if part) or None

    taken = _parse_exif_datetime(meta.get("DateTimeOriginal"))
    if taken is None:
        taken = _parse_exif_datetime(meta.get("CreateDate"))
    if taken is None:
        taken = _parse_exif_datetime(meta.get("MediaCreateDate"))

    duration = meta.get("Duration")
    return Item(
        path=path,
        kind=kind,
        bytes=size,
        device=device,
        taken=taken,
        utc_offset_minutes=_parse_offset(meta.get("OffsetTimeOriginal")),
        has_gps=meta.get("GPSLatitude") is not None and meta.get("GPSLongitude") is not None,
        duration=float(duration) if isinstance(duration, (int, float)) else None,
    )


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Avoids interpolating across a bimodal gap distribution."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def analyze(source: Path, items: list[Item], ignored: int, exiftool: bool) -> Profile:
    profile = Profile(source=source, ignored_files=ignored, exiftool_available=exiftool)

    gps_by_device: Counter[str] = Counter()
    for item in items:
        if item.kind is MediaKind.VIDEO:
            profile.videos += 1
            profile.video_seconds += item.duration or 0.0
        else:
            profile.images += 1
        profile.total_bytes += item.bytes
        profile.extensions[item.extension] += 1

        device = item.device or "(unknown device)"
        profile.devices[device] += 1
        if item.has_gps:
            gps_by_device[device] += 1
        else:
            profile.without_gps += 1

        if item.taken is None:
            profile.without_timestamp += 1
        if item.utc_offset_minutes is None:
            profile.offsets["(none)"] += 1
        else:
            profile.offsets[_format_offset(item.utc_offset_minutes)] += 1

    profile.device_gps = dict(gps_by_device)

    dated = sorted((i for i in items if i.taken is not None), key=lambda i: i.taken)
    if dated:
        profile.first = dated[0].taken
        profile.last = dated[-1].taken
        profile.local_dates = sorted({i.taken.date().isoformat() for i in dated})
        profile.late_night_items = sum(1 for i in dated if 0 <= i.taken.hour < 4)
        profile.gaps = _gap_stats(dated)
        profile.largest_day_gap_days = _largest_gap_days(dated)
        profile.timezone_crossings = _count_crossings(dated)

    return profile


def _format_offset(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def _gap_stats(dated: list[Item]) -> GapStats:
    """Gaps between consecutive items, which is what event_gap_minutes has to separate."""
    gaps = [
        (b.taken - a.taken).total_seconds() / 60.0 for a, b in zip(dated, dated[1:], strict=False)
    ]
    gaps = [g for g in gaps if g >= 0]
    if not gaps:
        return GapStats()
    return GapStats(
        count=len(gaps),
        p50=percentile(gaps, 0.50),
        p75=percentile(gaps, 0.75),
        p90=percentile(gaps, 0.90),
        p95=percentile(gaps, 0.95),
        p99=percentile(gaps, 0.99),
        largest=max(gaps),
    )


def _largest_gap_days(dated: list[Item]) -> float:
    largest = timedelta()
    for earlier, later in zip(dated, dated[1:], strict=False):
        largest = max(largest, later.taken - earlier.taken)
    return largest.total_seconds() / 86400.0


def _count_crossings(dated: list[Item]) -> int:
    """Changes in UTC offset over time -- each one is a day-boundary risk."""
    offsets = [i.utc_offset_minutes for i in dated if i.utc_offset_minutes is not None]
    return sum(1 for a, b in zip(offsets, offsets[1:], strict=False) if a != b)


def suggestions(profile: Profile) -> list[tuple[str, str, str]]:
    """(config key, suggested value, why). The reason for running this command."""
    out: list[tuple[str, str, str]] = []
    gaps = profile.gaps

    if gaps.count:
        # p90 separates "next shot at the same place" from "moved on"; round to a tidy number.
        candidate = max(30.0, min(240.0, gaps.p90))
        out.append(
            (
                "events.gap_minutes",
                f"{_round_to(candidate, 15):.0f}",
                f"p90 of {gaps.count} inter-photo gaps is {gaps.p90:.0f} min "
                f"(p50 {gaps.p50:.0f}, p99 {gaps.p99:.0f})",
            )
        )

    if profile.late_night_items:
        share = profile.late_night_items / max(1, profile.total)
        out.append(
            (
                "time.day_start_hour",
                "4",
                f"{profile.late_night_items} items ({share:.0%}) fall between 00:00 and 04:00; "
                "the default keeps them with the previous evening",
            )
        )

    if profile.largest_day_gap_days:
        suggested = max(1.0, profile.largest_day_gap_days + 0.5)
        note = (
            f"largest gap in the folder is {profile.largest_day_gap_days:.1f} days; "
            "set above it to avoid a spurious two-trips warning"
        )
        out.append(("time.suspicious_gap_days", f"{suggested:.1f}", note))

    if profile.without_gps:
        share = profile.without_gps / max(1, profile.total)
        out.append(
            (
                "time.gps_interpolation_window_minutes",
                "120",
                f"{profile.without_gps} items ({share:.0%}) lack GPS and need interpolation",
            )
        )

    if profile.videos:
        out.append(
            (
                "video.transcribe",
                "auto",
                f"{profile.videos} videos totaling {profile.video_seconds / 60:.0f} min; "
                "'all' would transcribe silent b-roll too",
            )
        )

    return out


def warnings(profile: Profile) -> list[str]:
    """Things that will produce wrong output if ignored."""
    out: list[str] = []

    if not profile.exiftool_available:
        out.append(
            "exiftool not found -- only file-level stats are available. "
            "Install it (`brew install exiftool`) for dates, GPS, and devices."
        )
    if not profile.total:
        out.append("no importable media found in this folder.")
        return out

    if profile.timezone_crossings:
        out.append(
            f"{profile.timezone_crossings} UTC-offset change(s) detected. Day boundaries and "
            "cross-device ordering depend on getting these right (T12)."
        )
    no_offset = profile.offsets.get("(none)", 0)
    if no_offset:
        share = no_offset / profile.total
        out.append(
            f"{no_offset} items ({share:.0%}) have no OffsetTimeOriginal tag; their timezone must "
            "come from GPS or config."
        )
    if profile.without_timestamp:
        out.append(
            f"{profile.without_timestamp} item(s) have no usable timestamp and cannot be placed "
            "on the timeline."
        )
    for device, count in profile.devices.items():
        with_gps = profile.device_gps.get(device, 0)
        if count >= 10 and with_gps == 0:
            out.append(
                f"'{device}' has {count} items and no GPS at all -- these depend entirely on "
                "interpolation from other devices (T20)."
            )
    if profile.largest_day_gap_days >= 3:
        out.append(
            f"largest gap is {profile.largest_day_gap_days:.1f} days, which may mean this folder "
            "contains more than one trip. This tool profiles one trip at a time."
        )
    return out


def _round_to(value: float, step: float) -> float:
    return round(value / step) * step


def run(source: Path) -> Profile:
    """Scan, read metadata, analyze. No database, no writes."""
    paths, ignored = scan(source)
    exiftool = shutil.which("exiftool") is not None
    metadata = read_metadata(paths)
    items = [build_item(p, metadata.get(str(p), {}), p.stat().st_size) for p in paths]
    return analyze(source, items, ignored, exiftool)
