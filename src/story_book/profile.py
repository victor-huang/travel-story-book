"""Phase 0 profiler: what is actually in a trip folder, and what the thresholds should be.

Deliberately standalone -- it touches no database and runs before the pipeline exists. Its
purpose is to replace the guessed defaults in `config.example.toml` with numbers observed from
real media, so every downstream stage is tuned against reality rather than intuition.

It reads EXIF itself rather than depending on the metadata stage (T11), because a diagnostic you
must build a database to run is a diagnostic nobody runs. The read here is intentionally
shallow: T11 owns real metadata extraction, timezone resolution, and persistence.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from story_book.db.models import MediaKind
from story_book.exif import (
    DEFAULT_CHUNK_SIZE,
    exiftool_available,
    extract_timestamp,
    run_exiftool,
)
from story_book.media_types import IGNORED_NAMES, classify, is_hidden

SUSTAINED_OFFSET_RUN = 3


@dataclass(slots=True)
class Item:
    """One scanned file with the shallow metadata the profiler needs."""

    path: Path
    kind: MediaKind
    bytes: int
    device: str | None = None
    taken: datetime | None = None
    time_source: str | None = None
    utc_offset_minutes: int | None = None
    has_gps: bool = False
    lat: float | None = None
    lon: float | None = None
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
    time_sources: Counter[str] = field(default_factory=Counter)
    offset_conflicts: int = 0
    conflict_examples: list[str] = field(default_factory=list)
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
    """Batch EXIF read, delegating to the canonical reader in `story_book.exif`."""
    if not paths or not exiftool_available():
        return {}
    return run_exiftool(paths, chunk_size=DEFAULT_CHUNK_SIZE)


def build_item(path: Path, meta: dict, size: int) -> Item:
    kind = classify(path) or MediaKind.IMAGE
    make = (meta.get("Make") or "").strip()
    model = (meta.get("Model") or "").strip()
    device = " ".join(part for part in (make, model) if part) or None

    # Field priority, parsing, and the embedded-offset fallback all live in `story_book.exif`.
    # The profiler used to carry its own copy; two implementations of a rule that real data just
    # corrected is exactly the kind of duplication that silently diverges.
    timestamp = extract_timestamp(meta, kind)

    latitude, longitude = meta.get("GPSLatitude"), meta.get("GPSLongitude")
    duration = meta.get("Duration")
    return Item(
        path=path,
        kind=kind,
        bytes=size,
        device=device,
        taken=timestamp.dt,
        time_source=timestamp.field,
        utc_offset_minutes=timestamp.offset_minutes,
        has_gps=latitude is not None and longitude is not None,
        lat=latitude if isinstance(latitude, (int, float)) else None,
        lon=longitude if isinstance(longitude, (int, float)) else None,
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
        else:
            profile.time_sources[item.time_source or "(unknown field)"] += 1
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
        conflicts = _offset_conflicts(dated)
        profile.offset_conflicts = len(conflicts)
        profile.conflict_examples = [i.path.name for i in conflicts[:5]]

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


def _count_crossings(dated: list[Item], min_run: int = SUSTAINED_OFFSET_RUN) -> int:
    """Sustained changes in UTC offset -- each one is a real day-boundary risk.

    Counts only offsets that hold for `min_run` consecutive items. A single mis-tagged photo
    otherwise reads as two crossings, and real libraries contain plenty of those: an edited or
    re-exported photo can carry the editing machine's offset rather than the camera's.
    """
    offsets = [i.utc_offset_minutes for i in dated if i.utc_offset_minutes is not None]
    if not offsets:
        return 0

    runs: list[int] = []
    for offset in offsets:
        if runs and runs[-1] == offset:
            continue
        runs.append(offset)

    sustained: list[int] = []
    index = 0
    while index < len(offsets):
        offset = offsets[index]
        length = 0
        while index + length < len(offsets) and offsets[index + length] == offset:
            length += 1
        if length >= min_run and (not sustained or sustained[-1] != offset):
            sustained.append(offset)
        index += length
    return max(0, len(sustained) - 1)


def _offset_conflicts(dated: list[Item]) -> list[Item]:
    """Items whose EXIF offset disagrees with the offset their GPS location implies.

    Real libraries contain these, and they matter: a photo taken in Vienna but tagged -07:00 is
    nine hours wrong, which lands it on the wrong day. The plan's original fallback order trusted
    OffsetTimeOriginal first; this check is the evidence that GPS must win a disagreement.
    """
    try:
        from timezonefinder import TimezoneFinder
    except ImportError:
        return []

    finder = TimezoneFinder()
    conflicts: list[Item] = []
    for item in dated:
        if item.utc_offset_minutes is None or item.lat is None or item.lon is None:
            continue
        zone_name = finder.timezone_at(lat=item.lat, lng=item.lon)
        if zone_name is None:
            continue
        try:
            offset = ZoneInfo(zone_name).utcoffset(item.taken)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        if offset is None:
            continue
        expected = int(offset.total_seconds() // 60)
        if expected != item.utc_offset_minutes:
            conflicts.append(item)
    return conflicts


def suggestions(profile: Profile) -> list[tuple[str, str, str]]:
    """(config key, suggested value, why). The reason for running this command."""
    out: list[tuple[str, str, str]] = []
    gaps = profile.gaps

    if gaps.count:
        # Basis is p95, not p90. Event boundaries are *rare* relative to shots-within-an-event:
        # a few hundred photos across a handful of days yield maybe 5% boundary gaps, so a p90
        # basis systematically over-splits. Observed on real data: p90 was 15 min and p95 44 min,
        # where the true boundaries sat near the latter.
        candidate = max(30.0, min(240.0, gaps.p95))
        out.append(
            (
                "events.gap_minutes",
                f"{_round_to(candidate, 15):.0f}",
                f"p95 of {gaps.count} inter-photo gaps is {gaps.p95:.0f} min "
                f"(p50 {gaps.p50:.0f}, p90 {gaps.p90:.0f}, p99 {gaps.p99:.0f})",
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
    if profile.offset_conflicts:
        share = profile.offset_conflicts / profile.total
        examples = ", ".join(profile.conflict_examples)
        out.append(
            f"{profile.offset_conflicts} item(s) ({share:.0%}) carry a UTC offset that disagrees "
            f"with what their GPS location implies (e.g. {examples}). Trust GPS over "
            "OffsetTimeOriginal for these -- an edited or re-exported photo can carry the "
            "editing machine's offset. Affects T12."
        )
    exported = profile.time_sources.get("CreateDate", 0) + profile.time_sources.get(
        "MediaCreateDate", 0
    )
    if exported:
        out.append(
            f"{exported} item(s) fall back to CreateDate/MediaCreateDate. On Photos-exported "
            "video these hold the *export* time, not the capture time. Verify before trusting "
            "their day assignment."
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
    exiftool = exiftool_available()
    metadata = read_metadata(paths)
    items = [build_item(p, metadata.get(str(p), {}), p.stat().st_size) for p in paths]
    return analyze(source, items, ignored, exiftool)
