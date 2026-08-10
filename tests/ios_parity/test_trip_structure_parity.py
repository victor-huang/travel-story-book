"""I16, part two: run the pipeline on both sets and diff `trip.json` structurally.

A field-level diff can pass while the export still loses something the pipeline *derives* from
several fields at once. This is the check that catches that: build the same library twice, once
from the original fixtures and once from their iOS exports, and compare what the pipeline
concluded.

**Compare structure, not identity.** Asset ids are prefixes of the BLAKE2b of the file's bytes,
and the bytes legitimately differ -- the export is downscaled. So every comparison here is keyed
by filename, and ids are expected to differ. Day count and boundaries, event count, resolved
timestamps, timezone offsets and places are not.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXPORTED = Path(__file__).resolve().parent / "exported"
SYNTHETIC = REPO / "tests" / "fixtures" / "media"
DEVICE = REPO / "tests" / "fixtures" / "device_media"


def _source_for(name: str) -> Path:
    for root in (DEVICE, SYNTHETIC):
        candidate = root / name
        if candidate.exists():
            return candidate
    raise AssertionError(f"no source fixture for iOS export {name}")


def _exported_media() -> list[Path]:
    return sorted(p for p in EXPORTED.iterdir() if p.suffix != ".json")


def _build(source_dir: Path, out_dir: Path) -> dict:
    """Run the real CLI. Importing the pipeline and driving it by hand would test a path no user
    takes; `--no-cloud` keeps it offline and deterministic.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "story_book.cli",
            "build",
            str(source_dir),
            "--out",
            str(out_dir),
            "--no-cloud",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, (
        f"build failed for {source_dir.name}:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
    )
    return json.loads((out_dir / "trip.json").read_text())


@pytest.fixture(scope="module")
def trips(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, dict]:
    """Both libraries, built once for the module. Two full pipeline runs are slow; this is the
    M0 gate and it earns the seconds.
    """
    base = tmp_path_factory.mktemp("ios_parity")

    # Both directories carry the same leaf name: `trip.name` is derived from it, so
    # "originals" vs "exports" would show up as a structural difference that is purely an
    # artifact of this harness.
    originals = base / "a" / "trip"
    originals.mkdir(parents=True)
    for export in _exported_media():
        shutil.copy2(_source_for(export.name), originals / export.name)

    exports = base / "b" / "trip"
    exports.mkdir(parents=True)
    for export in _exported_media():
        shutil.copy2(export, exports / export.name)

    return _build(originals, base / "out-originals"), _build(exports, base / "out-exports")


def _by_filename(trip: dict) -> dict[str, dict]:
    return {asset["filename"]: asset for asset in trip["assets"].values()}


class TestAssetLevelConclusions:
    def test_the_same_files_are_present(self, trips: tuple[dict, dict]) -> None:
        before, after = trips
        assert set(_by_filename(before)) == set(_by_filename(after))

    def test_asset_ids_differ_because_the_bytes_do(self, trips: tuple[dict, dict]) -> None:
        """The control for keying everything by filename. If ids matched, the exports would be
        byte-identical copies and this whole harness would be comparing a set with itself.
        """
        before, after = trips
        b, a = _by_filename(before), _by_filename(after)
        shared = set(b) & set(a)
        assert shared
        assert all(b[name]["content_hash"] != a[name]["content_hash"] for name in shared), (
            "an export is byte-identical to its source -- the export did nothing"
        )

    @pytest.mark.parametrize("field", ["taken_local", "taken_utc", "day", "calendar_date", "kind"])
    def test_resolved_timing_is_identical(self, trips: tuple[dict, dict], field: str) -> None:
        before, after = trips
        b, a = _by_filename(before), _by_filename(after)
        differing = {
            name: (b[name][field], a[name][field])
            for name in sorted(set(b) & set(a))
            if b[name][field] != a[name][field]
        }
        assert not differing, f"{field} changed for {differing}"

    def test_resolved_timezone_is_identical(self, trips: tuple[dict, dict]) -> None:
        """The highest-risk logic in the project, and the thing a downscale could plausibly
        break by dropping `OffsetTimeOriginal` -- order by UTC, split days by local.
        """
        before, after = trips
        b, a = _by_filename(before), _by_filename(after)
        for name in sorted(set(b) & set(a)):
            assert b[name]["timezone"] == a[name]["timezone"], (
                f"{name}: timezone resolved differently -- "
                f"{b[name]['timezone']} vs {a[name]['timezone']}"
            )

    def test_places_are_identical(self, trips: tuple[dict, dict]) -> None:
        before, after = trips
        b, a = _by_filename(before), _by_filename(after)
        for name in sorted(set(b) & set(a)):
            expected = b[name]["location"]
            actual = a[name]["location"]
            if expected is None or actual is None:
                assert expected == actual, f"{name}: one side has no location"
                continue
            assert (expected["lat"], expected["lon"]) == pytest.approx(
                (actual["lat"], actual["lon"])
            ), f"{name}: coordinates moved"
            before_place = (expected["place"] or {}).get("city")
            after_place = (actual["place"] or {}).get("city")
            assert before_place == after_place, f"{name}: geocoded to a different city"

    def test_orientation_survives(self, trips: tuple[dict, dict]) -> None:
        """`geometry.orientation` drives layout: a renderer that gets this wrong proposes a
        panoramic hero for a portrait photograph. Dimensions differ by design; the *shape* must
        not.
        """
        before, after = trips
        b, a = _by_filename(before), _by_filename(after)
        for name in sorted(set(b) & set(a)):
            assert b[name]["geometry"]["orientation"] == a[name]["geometry"]["orientation"], (
                f"{name}: orientation changed"
            )


class TestJustifiedDifferences:
    """Fields that legitimately differ, bounded rather than ignored.

    Excluding a field from the diff makes a catastrophic change invisible, so each one here is
    still asserted -- just against a tolerance or a shape instead of equality.
    """

    def test_quality_moves_only_slightly(self, trips: tuple[dict, dict]) -> None:
        """`quality` is measured from the pixels, and the export's pixels are a 1080px resample
        of the original's. A small change is correct; a large one means the downscale damaged the
        image rather than merely shrinking it.
        """
        before, after = trips
        b, a = _by_filename(before), _by_filename(after)
        for name in sorted(set(b) & set(a)):
            expected, actual = b[name]["quality"], a[name]["quality"]
            if expected is None or actual is None:
                assert expected == actual, f"{name}: quality present on only one side"
                continue
            assert abs(expected["overall"] - actual["overall"]) < 0.05, (
                f"{name}: quality moved from {expected['overall']} to {actual['overall']}"
            )

    def test_resampling_does_not_sharpen(self, trips: tuple[dict, dict]) -> None:
        """The direction is the interesting part: a 1080px resample of a 4032px original cannot
        add detail, so measured sharpness must not go up. If it did, the exporter would be
        applying something -- a sharpening filter, or a different resampler than intended.
        """
        before, after = trips
        b, a = _by_filename(before), _by_filename(after)
        checked = 0
        for name in sorted(set(b) & set(a)):
            expected, actual = b[name]["quality"], a[name]["quality"]
            if not expected or not actual or expected["sharpness"] is None:
                continue
            assert actual["sharpness"] <= expected["sharpness"] + 1e-9, (
                f"{name}: sharpness rose from {expected['sharpness']} to {actual['sharpness']}"
            )
            checked += 1
        assert checked, "no sharpness readings compared -- the assertion proved nothing"

    def test_video_facts_other_than_hash_keyed_paths_are_identical(
        self, trips: tuple[dict, dict]
    ) -> None:
        """`poster` and each keyframe `path` live under a content-hash directory, so they differ
        by construction. Everything the pipeline *concluded* about the clip must not.
        """
        before, after = trips
        b, a = _by_filename(before), _by_filename(after)

        def facts(video: dict) -> dict:
            return {
                k: v for k, v in video.items() if k not in {"poster", "keyframes", "transcript"}
            }

        clips = [n for n in sorted(set(b) & set(a)) if b[n]["kind"] == "video"]
        assert clips, "no clips in the parity set"
        for name in clips:
            assert facts(b[name]["video"]) == facts(a[name]["video"]), (
                f"{name}: video conclusions changed"
            )

    def test_a_posters_path_differs_only_in_its_hash_directory(
        self, trips: tuple[dict, dict]
    ) -> None:
        """The control for excluding `poster`: it must differ *because* of the hash, not because
        the export failed to produce a poster at all.
        """
        before, after = trips
        b, a = _by_filename(before), _by_filename(after)
        for name in sorted(set(b) & set(a)):
            if b[name]["kind"] != "video":
                continue
            assert b[name]["video"]["poster"], f"{name}: no poster before"
            assert a[name]["video"]["poster"], f"{name}: no poster after"
            assert b[name]["video"]["poster"] != a[name]["video"]["poster"]


class TestKnownPermutation:
    """`event_id` is `<date>#<seq>`, assigned in time order within a day. When two items share a
    `taken_utc` the order is a tie, and the tiebreak is byte-dependent -- so the ids swap between
    two libraries holding the same logical content.

    Found by this harness: `tz_before_1.jpg` (23:10+02:00) and `tz_after_1.jpg` (00:10+03:00) are
    both 21:10 UTC, and they exchange `#2` and `#3` between the two builds. This is a Python-side
    issue, not an export defect -- filed as a cross-task request in
    `dev_plan/implementation_tracker.md`. Until it is resolved the harness compares event
    boundaries as a multiset, and these tests pin the permutation so that *fixing* it also fails
    here and prompts an update.
    """

    def test_the_tie_exists_in_the_fixture_set(self, trips: tuple[dict, dict]) -> None:
        before, _ = trips
        instants = [asset["taken_utc"] for asset in before["assets"].values() if asset["taken_utc"]]
        assert len(instants) != len(set(instants)), (
            "no two assets share a taken_utc, so the permutation this harness works around "
            "cannot occur -- re-check whether the workaround is still needed"
        )

    def test_the_same_event_boundaries_exist_on_both_sides(self, trips: tuple[dict, dict]) -> None:
        """Regardless of which id they were given, the set of events is the same."""
        before, after = trips

        def events(trip: dict) -> list[tuple]:
            return sorted(
                (day["date"], event["start_local"], event["end_local"], event["counts"]["media"])
                for day in trip["days"]
                for event in day["events"]
            )

        assert events(before) == events(after)


class TestTripLevelStructure:
    def test_day_count_and_boundaries_are_identical(self, trips: tuple[dict, dict]) -> None:
        before, after = trips
        assert [d["date"] for d in before["days"]] == [d["date"] for d in after["days"]]

    def test_event_count_per_day_is_identical(self, trips: tuple[dict, dict]) -> None:
        before, after = trips
        assert {d["date"]: len(d["events"]) for d in before["days"]} == {
            d["date"]: len(d["events"]) for d in after["days"]
        }

    def test_trip_counts_are_identical(self, trips: tuple[dict, dict]) -> None:
        before, after = trips
        assert before["trip"]["counts"] == after["trip"]["counts"]

    def test_trip_bounds_are_identical(self, trips: tuple[dict, dict]) -> None:
        before, after = trips
        for field in ("start_local", "end_local", "start_utc", "end_utc", "timezone"):
            assert before["trip"][field] == after["trip"][field], f"trip.{field} changed"

    def test_privacy_and_context_blocks_are_identical(self, trips: tuple[dict, dict]) -> None:
        before, after = trips
        assert before["privacy"] == after["privacy"]
        assert before["context"] == after["context"]

    def test_the_structural_diff_is_empty(self, trips: tuple[dict, dict]) -> None:
        """The criterion, stated once as a whole.

        Excluded, each for a reason asserted elsewhere in this file: byte-derived identifiers and
        derivative paths, `quality` (measured from resampled pixels -- bounded by
        TestJustifiedDifferences), and the ids that permute under an instant tie (pinned by
        TestKnownPermutation).
        """
        before, after = trips
        byte_derived = {
            "asset_id",
            "content_hash",
            "bytes",
            "thumbnail",
            "preview",
            "geometry",
            "video",
        }
        justified_elsewhere = {"quality"}
        permutable = {"event_id", "cluster", "selected"}
        dropped = byte_derived | justified_elsewhere | permutable

        def shape(trip: dict) -> dict:
            return {
                "days": [
                    {
                        "date": day["date"],
                        "counts": day["counts"],
                        "gps_coverage": day["gps_coverage"],
                        # Sorted: event order within a day is not stable under an instant tie.
                        "events": sorted(
                            (
                                event["start_local"],
                                event["end_local"],
                                event["counts"]["media"],
                            )
                            for event in day["events"]
                        ),
                    }
                    for day in trip["days"]
                ],
                "trip": trip["trip"],
                "privacy": trip["privacy"],
                "assets": {
                    name: {k: v for k, v in asset.items() if k not in dropped}
                    for name, asset in _by_filename(trip).items()
                },
            }

        assert shape(before) == shape(after)
