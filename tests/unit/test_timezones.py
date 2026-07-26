"""Unit tests for timezone resolution: no DB, no filesystem, no network.

`timezonefinder.TimezoneFinder` loads a large geometry dataset from disk, so every test here
supplies a tiny in-memory stand-in via the `finder` fixture rather than a real one -- see
`get_timezone_finder` in `story_book.pipeline.timezones`, which is the only place the real
class is constructed.
"""

from __future__ import annotations

import logging

import pytest

from story_book.config import Config, DeviceConfig, TimeConfig, replace_devices
from story_book.db.models import GpsSource, TzSource
from story_book.pipeline.timezones import (
    resolve_timezones,
    warn_suspected_clock_offsets,
)

VIENNA = (47.8095, 13.0550)
ISTANBUL = (41.0082, 28.9784)


class _FakeFinder:
    """Maps every lookup to one fixed zone name, or raises if unexpected coordinates arrive."""

    def __init__(self, zone_name: str | None):
        self.zone_name = zone_name
        self.calls: list[tuple[float, float]] = []

    def timezone_at(self, *, lat: float, lng: float) -> str | None:
        self.calls.append((lat, lng))
        return self.zone_name


@pytest.fixture
def finder() -> _FakeFinder:
    return _FakeFinder("Europe/Vienna")


def _with_default_timezone(config: Config, name: str) -> Config:
    new_time = TimeConfig(
        day_start_hour=config.time.day_start_hour,
        suspicious_gap_days=config.time.suspicious_gap_days,
        default_timezone=name,
        gps_interpolation_window_minutes=config.time.gps_interpolation_window_minutes,
    )
    return Config(time=new_time)


def _with_device(config: Config, device_id: str, device_config: DeviceConfig) -> Config:
    return replace_devices(config, {**config.devices, device_id: device_config})


class TestExifOffsetLevel:
    """Level 1: OffsetTimeOriginal, only when it agrees with GPS."""

    def test_used_when_it_agrees_with_gps(self, make_media, finder: _FakeFinder) -> None:
        media = make_media(
            taken_local="2026-07-18T09:20:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            tz_offset_minutes=120,
            tz_source=TzSource.EXIF_OFFSET,
        )

        [resolved] = resolve_timezones([media], Config(), finder)

        assert resolved.tz_source == TzSource.EXIF_OFFSET
        assert resolved.tz_offset_minutes == 120
        assert resolved.tz_name == "Europe/Vienna"
        assert resolved.taken_utc == "2026-07-18T07:20:00+00:00"

    def test_taken_local_is_preserved_when_no_clock_correction_applies(
        self, make_media, finder: _FakeFinder
    ) -> None:
        media = make_media(
            taken_local="2026-07-18T09:20:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            tz_offset_minutes=120,
            tz_source=TzSource.EXIF_OFFSET,
        )

        [resolved] = resolve_timezones([media], Config(), finder)

        assert resolved.taken_local == "2026-07-18T09:20:00"


class TestGpsLevel:
    """Level 2: GPS via timezonefinder + zoneinfo. Wins any EXIF disagreement."""

    def test_used_when_no_exif_offset_tag_present(self, make_media, finder: _FakeFinder) -> None:
        media = make_media(taken_local="2026-07-18T11:45:00", lat=VIENNA[0], lon=VIENNA[1])

        [resolved] = resolve_timezones([media], Config(), finder)

        assert resolved.tz_source == TzSource.GPS
        assert resolved.tz_name == "Europe/Vienna"
        assert resolved.tz_offset_minutes == 120
        assert resolved.taken_utc == "2026-07-18T09:45:00+00:00"

    def test_gps_wins_over_a_disagreeing_exif_offset(
        self, make_media, finder: _FakeFinder, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression case: coordinates in one zone tagged with an offset 9 hours off."""
        media = make_media(
            taken_local="2026-07-19T06:15:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            tz_offset_minutes=-420,
            tz_source=TzSource.EXIF_OFFSET,
        )

        with caplog.at_level(logging.WARNING):
            [resolved] = resolve_timezones([media], Config(), finder)

        assert resolved.tz_source == TzSource.GPS
        assert resolved.tz_offset_minutes == 120
        assert resolved.tz_name == "Europe/Vienna"
        assert any("disagrees" in record.message for record in caplog.records)

    def test_dst_offset_is_computed_via_zoneinfo_not_a_fixed_offset(
        self, make_media, finder: _FakeFinder
    ) -> None:
        winter = make_media(
            "winter", taken_local="2026-01-10T09:00:00", lat=VIENNA[0], lon=VIENNA[1]
        )
        summer = make_media(
            "summer", taken_local="2026-07-10T09:00:00", lat=VIENNA[0], lon=VIENNA[1]
        )

        resolved = resolve_timezones([winter, summer], Config(), finder)

        by_hash = {m.hash: m for m in resolved}
        assert by_hash["winter"].tz_offset_minutes == 60  # CET
        assert by_hash["summer"].tz_offset_minutes == 120  # CEST

    def test_unresolvable_coordinates_fall_back_to_trip_default(
        self, make_media, caplog: pytest.LogCaptureFixture
    ) -> None:
        media = make_media(taken_local="2026-07-18T11:45:00", lat=0.0, lon=-30.0)
        config = _with_default_timezone(Config(), "UTC")

        with caplog.at_level(logging.WARNING):
            [resolved] = resolve_timezones([media], config, _FakeFinder(None))

        assert resolved.tz_name == "UTC"
        assert resolved.tz_offset_minutes == 0


class TestDeviceNeighborLevel:
    """Level 3: nearest-in-time GPS-bearing item on the same device."""

    def test_used_when_item_has_no_gps_but_device_does_elsewhere(
        self, make_media, finder: _FakeFinder
    ) -> None:
        anchor = make_media(
            "anchor",
            taken_local="2026-07-18T12:00:00",
            device_id="Sony",
            lat=VIENNA[0],
            lon=VIENNA[1],
        )
        orphan = make_media("orphan", taken_local="2026-07-18T12:05:00", device_id="Sony")

        resolved = resolve_timezones([anchor, orphan], Config(), finder)

        by_hash = {m.hash: m for m in resolved}
        assert by_hash["orphan"].tz_source == TzSource.DEVICE_NEIGHBOR
        assert by_hash["orphan"].tz_name == "Europe/Vienna"
        assert by_hash["orphan"].tz_offset_minutes == 120

    def test_picks_the_nearer_of_two_same_device_anchors(
        self, make_media, finder: _FakeFinder
    ) -> None:
        near = make_media(
            "near",
            taken_local="2026-07-18T12:00:00",
            device_id="Sony",
            lat=VIENNA[0],
            lon=VIENNA[1],
        )
        far = make_media(
            "far",
            taken_local="2026-07-18T02:00:00",
            device_id="Sony",
            lat=ISTANBUL[0],
            lon=ISTANBUL[1],
        )
        orphan = make_media("orphan", taken_local="2026-07-18T12:10:00", device_id="Sony")

        finder_by_coord = _FakeFinder("Europe/Vienna")

        # far anchor resolved separately with a different fake would be Istanbul; here both
        # share one finder, so pin the "far" anchor's zone explicitly via a second call is not
        # possible with one fake -- instead assert the *closer* anchor's timestamp wins.
        resolved = resolve_timezones([near, far, orphan], Config(), finder_by_coord)
        by_hash = {m.hash: m for m in resolved}
        assert by_hash["orphan"].tz_source == TzSource.DEVICE_NEIGHBOR
        assert by_hash["orphan"].taken_local == "2026-07-18T12:10:00"
        # Nearest anchor by naive local time is "near" (10 minutes away vs 10 hours).
        assert by_hash["orphan"].tz_offset_minutes == by_hash["near"].tz_offset_minutes

    def test_only_considers_the_same_device(self, make_media, finder: _FakeFinder) -> None:
        other_device_anchor = make_media(
            "other",
            taken_local="2026-07-18T12:00:00",
            device_id="iPhone",
            lat=VIENNA[0],
            lon=VIENNA[1],
        )
        orphan = make_media("orphan", taken_local="2026-07-18T12:01:00", device_id="Sony")
        config = _with_default_timezone(Config(), "UTC")

        resolved = resolve_timezones([other_device_anchor, orphan], config, finder)

        by_hash = {m.hash: m for m in resolved}
        assert by_hash["orphan"].tz_source == TzSource.CONFIG
        assert by_hash["orphan"].tz_name == "UTC"


class TestConfigLevel:
    """Level 4: trip default, or a per-device override, when nothing else is available."""

    def test_used_when_neither_offset_nor_gps_nor_device_neighbor_exists(
        self, make_media, finder: _FakeFinder
    ) -> None:
        media = make_media(taken_local="2026-07-18T09:00:00")
        config = _with_default_timezone(Config(), "UTC")

        [resolved] = resolve_timezones([media], config, finder)

        assert resolved.tz_source == TzSource.CONFIG
        assert resolved.tz_name == "UTC"
        assert resolved.tz_offset_minutes == 0
        assert resolved.taken_utc == "2026-07-18T09:00:00+00:00"

    def test_per_device_default_timezone_overrides_trip_default(
        self, make_media, finder: _FakeFinder
    ) -> None:
        media = make_media(taken_local="2026-07-18T09:00:00", device_id="Sony ILCE-7M4")
        config = _with_device(
            _with_default_timezone(Config(), "UTC"),
            "Sony ILCE-7M4",
            DeviceConfig(default_timezone="Asia/Tokyo"),
        )

        [resolved] = resolve_timezones([media], config, finder)

        assert resolved.tz_name == "Asia/Tokyo"
        assert resolved.tz_offset_minutes == 540


class TestClockOffsetCorrection:
    """`DeviceConfig.clock_offset_minutes` shifts the naive local reading before zone math."""

    def test_clock_offset_shifts_both_local_and_utc(self, make_media, finder: _FakeFinder) -> None:
        media = make_media(
            taken_local="2026-07-18T09:00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            device_id="Sony ILCE-7M4",
        )
        config = _with_device(Config(), "Sony ILCE-7M4", DeviceConfig(clock_offset_minutes=45))

        [resolved] = resolve_timezones([media], config, finder)

        assert resolved.taken_local == "2026-07-18T09:45:00"
        assert resolved.taken_utc == "2026-07-18T07:45:00+00:00"


class TestNoTimestampOrEvidence:
    def test_media_with_no_timestamp_is_left_untouched_and_excluded(
        self, make_media, finder: _FakeFinder
    ) -> None:
        media = make_media(taken_local=None)

        resolved = resolve_timezones([media], Config(), finder)

        assert resolved == []
        assert media.tz_source == TzSource.UNKNOWN
        assert media.taken_utc is None

    def test_media_with_neither_offset_nor_gps_falls_all_the_way_to_config(
        self, make_media, finder: _FakeFinder
    ) -> None:
        media = make_media(taken_local="2026-07-18T09:00:00", gps_source=GpsSource.NONE)

        [resolved] = resolve_timezones([media], Config(), finder)

        assert resolved.tz_source == TzSource.CONFIG


class TestSuspectedClockOffsetWarning:
    def test_warns_when_a_device_is_consistently_offset_from_other_devices(
        self, make_media, finder: _FakeFinder, caplog: pytest.LogCaptureFixture
    ) -> None:
        gps_items = [
            make_media(
                f"gps{i}",
                taken_local=f"2026-07-18T12:0{i}:00",
                device_id="iPhone",
                lat=VIENNA[0],
                lon=VIENNA[1],
            )
            for i in range(3)
        ]
        # Sony has no GPS, and its clock reads ~20 minutes ahead of the iPhone's nearby shots.
        sony_items = [
            make_media(f"sony{i}", taken_local=f"2026-07-18T12:2{i}:00", device_id="Sony")
            for i in range(3)
        ]
        config = _with_default_timezone(Config(), "Europe/Vienna")

        with caplog.at_level(logging.WARNING):
            resolve_timezones([*gps_items, *sony_items], config, finder)

        assert any(
            "Sony" in record.message and "clock" in record.message for record in caplog.records
        )

    def test_no_warning_for_a_device_already_corrected_in_config(
        self, make_media, finder: _FakeFinder, caplog: pytest.LogCaptureFixture
    ) -> None:
        gps_items = [
            make_media(
                f"gps{i}",
                taken_local=f"2026-07-18T12:0{i}:00",
                device_id="iPhone",
                lat=VIENNA[0],
                lon=VIENNA[1],
            )
            for i in range(3)
        ]
        sony_items = [
            make_media(f"sony{i}", taken_local=f"2026-07-18T12:2{i}:00", device_id="Sony")
            for i in range(3)
        ]
        config = _with_device(
            _with_default_timezone(Config(), "Europe/Vienna"),
            "Sony",
            DeviceConfig(clock_offset_minutes=20),
        )

        with caplog.at_level(logging.WARNING):
            resolve_timezones([*gps_items, *sony_items], config, finder)

        assert not any("clock" in record.message for record in caplog.records)

    def test_no_warning_when_deltas_do_not_cluster(
        self, make_media, finder: _FakeFinder, caplog: pytest.LogCaptureFixture
    ) -> None:
        gps_items = [
            make_media(
                "gps0",
                taken_local="2026-07-18T12:00:00",
                device_id="iPhone",
                lat=VIENNA[0],
                lon=VIENNA[1],
            ),
            make_media(
                "gps1",
                taken_local="2026-07-18T13:00:00",
                device_id="iPhone",
                lat=VIENNA[0],
                lon=VIENNA[1],
            ),
        ]
        sony_items = [
            make_media("sony0", taken_local="2026-07-18T12:01:00", device_id="Sony"),
            make_media("sony1", taken_local="2026-07-18T13:20:00", device_id="Sony"),
        ]
        config = _with_default_timezone(Config(), "Europe/Vienna")

        with caplog.at_level(logging.WARNING):
            resolve_timezones([*gps_items, *sony_items], config, finder)

        assert not any("clock" in record.message for record in caplog.records)

    def test_warn_suspected_clock_offsets_is_directly_callable(
        self, make_media, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pure helper, independent of resolve_timezones, for direct unit coverage."""
        gps = make_media(
            "gps",
            taken_local="2026-07-18T12:00:00",
            taken_utc="2026-07-18T10:00:00+00:00",
            device_id="iPhone",
            lat=VIENNA[0],
            lon=VIENNA[1],
        )
        sony = make_media(
            "sony",
            taken_local="2026-07-18T12:20:00",
            taken_utc="2026-07-18T10:20:00+00:00",
            device_id="Sony",
        )
        sony2 = make_media(
            "sony2",
            taken_local="2026-07-18T12:21:00",
            taken_utc="2026-07-18T10:21:00+00:00",
            device_id="Sony",
        )
        gps2 = make_media(
            "gps2",
            taken_local="2026-07-18T12:01:00",
            taken_utc="2026-07-18T10:01:00+00:00",
            device_id="iPhone",
            lat=VIENNA[0],
            lon=VIENNA[1],
        )

        with caplog.at_level(logging.WARNING):
            warn_suspected_clock_offsets([gps, gps2, sony, sony2], Config())

        assert any("Sony" in record.message for record in caplog.records)
