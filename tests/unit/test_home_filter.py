"""Unit tests for the home-location privacy filter: no DB, no filesystem, no network.

Exercises the pure distance math (`haversine_km`, `is_within_home_radius`) and the export
predicate (`should_exclude_from_export`) directly against in-memory `Media` objects.
"""

from __future__ import annotations

from story_book.config import HomeLocation
from story_book.db.models import GpsSource, Media, MediaKind
from story_book.pipeline.home_filter import (
    haversine_km,
    is_within_home_radius,
    should_exclude_from_export,
    unknown_location_count,
)

VIENNA = (48.2082, 16.3738)
ISTANBUL = (41.0082, 28.9784)


class TestHaversineKm:
    """Great-circle distance against a known real-world figure."""

    def test_zero_distance_between_identical_points(self) -> None:
        assert haversine_km(*VIENNA, *VIENNA) == 0.0

    def test_vienna_to_istanbul_matches_known_great_circle_distance(self) -> None:
        distance = haversine_km(*VIENNA, *ISTANBUL)

        # The real great-circle distance between these two cities is ~1270 km. Euclidean
        # distance on raw degrees would be wildly wrong at this latitude, so a tight tolerance
        # here also guards against silently swapping in the wrong formula.
        assert 1250 < distance < 1290

    def test_distance_is_symmetric(self) -> None:
        assert haversine_km(*VIENNA, *ISTANBUL) == haversine_km(*ISTANBUL, *VIENNA)


class TestIsWithinHomeRadius:
    """Distance test against a configured home, used to compute `is_near_home`."""

    def test_item_inside_the_radius_is_flagged(self, make_media) -> None:
        home = HomeLocation(lat=VIENNA[0], lon=VIENNA[1], exclusion_km=5.0)
        media = make_media(lat=VIENNA[0], lon=VIENNA[1])

        assert is_within_home_radius(media, home) is True

    def test_item_outside_the_radius_is_not_flagged(self, make_media) -> None:
        home = HomeLocation(lat=VIENNA[0], lon=VIENNA[1], exclusion_km=5.0)
        media = make_media(lat=ISTANBUL[0], lon=ISTANBUL[1])

        assert is_within_home_radius(media, home) is False

    def test_boundary_distance_is_inclusive(self, make_media) -> None:
        # Move due north from home by exactly exclusion_km, using the small-angle approximation
        # that 1 degree of latitude spans ~111.19 km -- close enough over a few km to land the
        # computed haversine distance within a hair of the configured radius.
        exclusion_km = 5.0
        delta_lat = exclusion_km / 111.19
        home = HomeLocation(lat=VIENNA[0], lon=VIENNA[1], exclusion_km=exclusion_km)
        media = make_media(lat=VIENNA[0] + delta_lat, lon=VIENNA[1])
        boundary_distance = haversine_km(media.lat, media.lon, home.lat, home.lon)

        # Bump the configured radius up to exactly the measured distance so this test is not
        # sensitive to the small-angle approximation's error -- it asserts the `<=` semantics,
        # not the geodesy.
        home_exact = HomeLocation(lat=home.lat, lon=home.lon, exclusion_km=boundary_distance)

        assert is_within_home_radius(media, home_exact) is True

    def test_item_without_coordinates_is_not_flagged(self, make_media) -> None:
        home = HomeLocation(lat=VIENNA[0], lon=VIENNA[1], exclusion_km=5.0)
        media = make_media(lat=None, lon=None)

        assert is_within_home_radius(media, home) is False

    def test_interpolated_coordinates_near_home_are_flagged(self, make_media) -> None:
        home = HomeLocation(lat=VIENNA[0], lon=VIENNA[1], exclusion_km=5.0)
        media = make_media(lat=VIENNA[0], lon=VIENNA[1], gps_source=GpsSource.INTERPOLATED)

        assert is_within_home_radius(media, home) is True


class TestShouldExcludeFromExport:
    """The predicate exports must call -- fails toward privacy on missing coordinates."""

    def test_item_flagged_near_home_is_excluded(self, make_media) -> None:
        media = make_media(lat=VIENNA[0], lon=VIENNA[1], is_near_home=True)

        assert (
            should_exclude_from_export(
                media, HomeLocation(lat=48.2082, lon=16.3738, exclusion_km=5.0)
            )
            is True
        )

    def test_item_far_from_home_with_known_location_is_not_excluded(self, make_media) -> None:
        media = make_media(lat=ISTANBUL[0], lon=ISTANBUL[1], is_near_home=False)

        assert (
            should_exclude_from_export(
                media, HomeLocation(lat=48.2082, lon=16.3738, exclusion_km=5.0)
            )
            is False
        )

    def test_item_with_no_coordinates_is_excluded_even_though_unflagged(self, make_media) -> None:
        media = make_media(lat=None, lon=None, is_near_home=False)

        assert (
            should_exclude_from_export(
                media, HomeLocation(lat=48.2082, lon=16.3738, exclusion_km=5.0)
            )
            is True
        )


class TestUnknownLocationDependsOnWhetherHomeIsConfigured:
    """The over-cautious direction of the same silent-failure family.

    An unconditional "exclude anything without coordinates" protects nothing when no home is set,
    and quietly deletes real content: the plan's input list includes a Sony camera and a GoPro,
    neither of which records GPS, so a DSLR-heavy trip whose gaps exceed the interpolation window
    would lose those photos from the book with no error at all.
    """

    def _no_location(self) -> Media:
        return Media(hash="h", path="/x.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)

    def test_without_a_home_an_unlocated_item_is_kept(self) -> None:
        assert should_exclude_from_export(self._no_location(), None) is False

    def test_with_a_home_an_unlocated_item_is_excluded(self) -> None:
        home = HomeLocation(lat=48.2082, lon=16.3738, exclusion_km=5.0)
        assert should_exclude_from_export(self._no_location(), home) is True

    def test_a_flagged_item_is_excluded_even_without_a_home_configured(self) -> None:
        """If a previous run flagged it, that judgement stands regardless of current config."""
        flagged = Media(
            hash="h",
            path="/x.jpg",
            kind=MediaKind.IMAGE,
            bytes=1,
            mtime=0.0,
            lat=48.2082,
            lon=16.3738,
            is_near_home=True,
        )
        assert should_exclude_from_export(flagged, None) is True

    def test_a_located_item_far_from_home_is_kept(self) -> None:
        home = HomeLocation(lat=48.2082, lon=16.3738, exclusion_km=5.0)
        far = Media(
            hash="h",
            path="/x.jpg",
            kind=MediaKind.IMAGE,
            bytes=1,
            mtime=0.0,
            lat=41.0082,
            lon=28.9784,
        )
        assert should_exclude_from_export(far, home) is False

    def test_the_droppable_count_is_reportable(self) -> None:
        """Callers must be able to say how many photos an export would drop, never hide it."""
        items = [self._no_location(), self._no_location()]
        items.append(
            Media(
                hash="c", path="/c.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0, lat=1.0, lon=1.0
            )
        )
        assert unknown_location_count(items) == 2
