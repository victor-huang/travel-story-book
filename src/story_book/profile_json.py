"""JSON form of a profile, so P01's numbers can be diffed and pasted rather than re-eyeballed."""

from __future__ import annotations

from typing import Any

from story_book.profile import Profile, suggestions, warnings


def profile_to_dict(profile: Profile) -> dict[str, Any]:
    return {
        "source": str(profile.source),
        "exiftool_available": profile.exiftool_available,
        "media": {
            "images": profile.images,
            "videos": profile.videos,
            "total": profile.total,
            "total_bytes": profile.total_bytes,
            "ignored_files": profile.ignored_files,
            "heic_share": round(profile.heic_share, 4),
            "extensions": dict(profile.extensions.most_common()),
            "video_seconds": round(profile.video_seconds, 1),
        },
        "devices": {
            name: {"items": count, "with_gps": profile.device_gps.get(name, 0)}
            for name, count in profile.devices.most_common()
        },
        "time": {
            "first": profile.first.isoformat() if profile.first else None,
            "last": profile.last.isoformat() if profile.last else None,
            "span_days": profile.span_days,
            "dates_with_media": len(profile.local_dates),
            "without_timestamp": profile.without_timestamp,
            "utc_offsets": dict(profile.offsets.most_common()),
            "offset_changes": profile.timezone_crossings,
            "largest_gap_days": round(profile.largest_day_gap_days, 3),
            "timestamp_sources": dict(profile.time_sources.most_common()),
            "offset_gps_conflicts": profile.offset_conflicts,
            "offset_gps_conflict_examples": profile.conflict_examples,
            "late_night_items": profile.late_night_items,
            "gaps_minutes": {
                "count": profile.gaps.count,
                "p50": round(profile.gaps.p50, 2),
                "p75": round(profile.gaps.p75, 2),
                "p90": round(profile.gaps.p90, 2),
                "p95": round(profile.gaps.p95, 2),
                "p99": round(profile.gaps.p99, 2),
                "largest": round(profile.gaps.largest, 2),
            },
        },
        "location": {
            "without_gps": profile.without_gps,
            "gps_coverage": round(profile.gps_coverage, 4),
        },
        "warnings": warnings(profile),
        "suggested_config": [
            {"key": key, "value": value, "basis": why} for key, value, why in suggestions(profile)
        ],
    }
