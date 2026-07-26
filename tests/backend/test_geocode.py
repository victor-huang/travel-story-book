"""Backend tests for reverse geocoding: real temp DB, real offline dataset.

`reverse-geocoder`'s bundled GeoNames extract is local data (no network, no download), so using
the real thing here -- rather than a fake k-d tree -- is encouraged and lets these tests assert
real expected cities for the three fixture coordinates (Vienna, Salzburg, Istanbul). Nominatim
is never really called: every test that enables it mocks `NominatimClient._call`, and one test
proves `urllib.request.urlopen` itself is never reached.
"""

from __future__ import annotations

import sqlite3

import pytest

from story_book.config import Config, GeocodeConfig
from story_book.db import connection as db
from story_book.db.connection import get_media
from story_book.db.models import GpsSource
from story_book.pipeline.base import StageContext
from story_book.pipeline.geocode import (
    GeocodeStage,
    NominatimClient,
    candidate_places,
    reverse_geocode_offline,
)

VIENNA = (48.2082, 16.3738)
SALZBURG = (47.8095, 13.0550)
ISTANBUL = (41.0082, 28.9784)
MID_OCEAN = (0.0, -30.0)


def _place_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM place").fetchall())


class TestRealFixtureCoordinates:
    """Sanity check against the real bundled dataset -- no fake geocoder involved."""

    def test_vienna_resolves_to_vienna(self) -> None:
        result = reverse_geocode_offline(*VIENNA)

        assert result.name == "Vienna"
        assert result.country_code == "AT"

    def test_salzburg_resolves_to_salzburg(self) -> None:
        result = reverse_geocode_offline(*SALZBURG)

        assert result.name == "Salzburg"
        assert result.country_code == "AT"

    def test_istanbul_resolves_within_istanbul_province(self) -> None:
        result = reverse_geocode_offline(*ISTANBUL)

        assert result.admin1 == "Istanbul"
        assert result.country_code == "TR"

    def test_candidate_places_returns_several_options_with_distances(self) -> None:
        candidates = candidate_places(*VIENNA, count=3)

        assert len(candidates) == 3
        assert all(c.distance_m >= 0 for c in candidates)
        # Nearest-first.
        assert candidates[0].distance_m <= candidates[-1].distance_m


class TestCoordinateRounding:
    """Hundreds of photos at one spot must resolve to one `place` row."""

    def test_nearby_items_collapse_to_one_place_row(self, ctx, make_media) -> None:
        lat, lon = VIENNA
        for i in range(5):
            jitter = i * 1e-6  # far tighter than the 4-decimal rounding key
            media = make_media(f"vienna-{i}", lat=lat + jitter, lon=lon + jitter)
            from story_book.db.connection import upsert_media

            upsert_media(ctx.conn, media)

        GeocodeStage().run(ctx)

        rows = _place_rows(ctx.conn)
        assert len(rows) == 1
        assert rows[0]["city"] == "Vienna"

    def test_all_items_share_the_same_place_id(self, ctx, make_media) -> None:
        lat, lon = VIENNA
        from story_book.db.connection import upsert_media

        for i in range(3):
            upsert_media(ctx.conn, make_media(f"vienna-{i}", lat=lat, lon=lon + i * 1e-7))

        GeocodeStage().run(ctx)

        place_ids = {get_media(ctx.conn, f"vienna-{i}").place_id for i in range(3)}
        assert len(place_ids) == 1
        assert None not in place_ids

    def test_distinct_cities_get_distinct_place_rows(self, ctx, make_media) -> None:
        from story_book.db.connection import upsert_media

        upsert_media(ctx.conn, make_media("vienna", lat=VIENNA[0], lon=VIENNA[1]))
        upsert_media(ctx.conn, make_media("salzburg", lat=SALZBURG[0], lon=SALZBURG[1]))

        GeocodeStage().run(ctx)

        vienna_place = get_media(ctx.conn, "vienna").place_id
        salzburg_place = get_media(ctx.conn, "salzburg").place_id
        assert vienna_place != salzburg_place


class TestCachePreventsSecondLookup:
    """The offline geocoder is queried once per distinct rounded coordinate, ever."""

    def test_second_item_at_same_coordinate_costs_no_extra_lookup(
        self, ctx, make_media, mocker
    ) -> None:
        from story_book.db.connection import upsert_media

        spy = mocker.patch(
            "story_book.pipeline.geocode.reverse_geocode_offline",
            wraps=reverse_geocode_offline,
        )
        upsert_media(ctx.conn, make_media("a", lat=VIENNA[0], lon=VIENNA[1]))
        upsert_media(ctx.conn, make_media("b", lat=VIENNA[0], lon=VIENNA[1]))
        upsert_media(ctx.conn, make_media("c", lat=VIENNA[0], lon=VIENNA[1]))

        GeocodeStage().run(ctx)

        assert spy.call_count == 1

    def test_a_second_run_reuses_the_place_table_and_looks_up_nothing_new(
        self, ctx, make_media, mocker
    ) -> None:
        from story_book.db.connection import upsert_media

        upsert_media(ctx.conn, make_media("a", lat=VIENNA[0], lon=VIENNA[1]))
        GeocodeStage().run(ctx)

        spy = mocker.patch(
            "story_book.pipeline.geocode.reverse_geocode_offline",
            wraps=reverse_geocode_offline,
        )
        upsert_media(ctx.conn, make_media("b", lat=VIENNA[0], lon=VIENNA[1]))
        GeocodeStage().run(ctx)

        spy.assert_not_called()
        assert len(_place_rows(ctx.conn)) == 1


class TestItemsWithoutGpsAreSkipped:
    def test_place_id_stays_none(self, ctx, make_media) -> None:
        from story_book.db.connection import upsert_media

        upsert_media(ctx.conn, make_media("no-gps"))

        GeocodeStage().run(ctx)

        assert get_media(ctx.conn, "no-gps").place_id is None
        assert _place_rows(ctx.conn) == []

    def test_gps_item_alongside_a_gps_less_one_still_resolves(self, ctx, make_media) -> None:
        from story_book.db.connection import upsert_media

        upsert_media(ctx.conn, make_media("no-gps"))
        upsert_media(ctx.conn, make_media("has-gps", lat=VIENNA[0], lon=VIENNA[1]))

        GeocodeStage().run(ctx)

        assert get_media(ctx.conn, "no-gps").place_id is None
        assert get_media(ctx.conn, "has-gps").place_id is not None


class TestNominatimDisabledByDefault:
    """Zero network calls unless explicitly opted in -- the acceptance criterion's core claim."""

    def test_nominatim_client_is_never_constructed(self, ctx, make_media, mocker) -> None:
        from story_book.db.connection import upsert_media

        ctor = mocker.patch("story_book.pipeline.geocode.NominatimClient")
        upsert_media(ctx.conn, make_media("a", lat=VIENNA[0], lon=VIENNA[1]))

        GeocodeStage().run(ctx)

        ctor.assert_not_called()

    def test_network_is_never_touched(self, ctx, make_media, mocker) -> None:
        from story_book.db.connection import upsert_media

        urlopen = mocker.patch("urllib.request.urlopen")
        upsert_media(ctx.conn, make_media("a", lat=VIENNA[0], lon=VIENNA[1]))

        GeocodeStage().run(ctx)

        urlopen.assert_not_called()

    def test_place_gets_a_city_level_label_with_no_poi(self, ctx, make_media) -> None:
        from story_book.db.connection import upsert_media

        upsert_media(ctx.conn, make_media("a", lat=VIENNA[0], lon=VIENNA[1]))

        GeocodeStage().run(ctx)

        [place] = _place_rows(ctx.conn)
        assert place["source"] == "offline"
        assert place["city"] == "Vienna"
        assert place["poi"] is None

    def test_no_cloud_overrides_a_config_that_enabled_nominatim(
        self, conn, out_dir, source_dir, make_media, mocker
    ) -> None:
        """`--no-cloud` must win even if a config file left `use_nominatim` on."""
        from story_book.db.connection import upsert_media

        config = Config(geocode=GeocodeConfig(use_nominatim=True))
        no_cloud_ctx = StageContext(
            conn=conn, config=config, out_dir=out_dir, source_dir=source_dir, no_cloud=True
        )
        ctor = mocker.patch("story_book.pipeline.geocode.NominatimClient")
        upsert_media(conn, make_media("a", lat=VIENNA[0], lon=VIENNA[1]))

        GeocodeStage().run(no_cloud_ctx)

        ctor.assert_not_called()


class TestNominatimRateLimiter:
    """Enabled explicitly, POI lookups are rate-limited per Nominatim's usage policy."""

    def test_second_distinct_coordinate_within_the_window_sleeps(
        self, conn, out_dir, source_dir, make_media, mocker
    ) -> None:
        from story_book.db.connection import upsert_media

        config = Config(
            geocode=GeocodeConfig(use_nominatim=True, nominatim_min_interval_seconds=1.1)
        )
        rate_limited_ctx = StageContext(
            conn=conn, config=config, out_dir=out_dir, source_dir=source_dir
        )
        mocker.patch.object(NominatimClient, "_call", return_value={"name": "Some POI"})
        sleep = mocker.patch("story_book.pipeline.geocode.time.sleep")
        mocker.patch("story_book.pipeline.geocode.time.monotonic", side_effect=[100.0, 100.4])
        upsert_media(conn, make_media("vienna", lat=VIENNA[0], lon=VIENNA[1]))
        upsert_media(conn, make_media("salzburg", lat=SALZBURG[0], lon=SALZBURG[1]))

        GeocodeStage().run(rate_limited_ctx)

        sleep.assert_called_once()
        (waited,) = sleep.call_args.args
        assert waited == pytest.approx(0.7, abs=1e-6)

    def test_poi_is_stored_when_nominatim_is_enabled(
        self, conn, out_dir, source_dir, make_media, mocker
    ) -> None:
        from story_book.db.connection import upsert_media

        config = Config(geocode=GeocodeConfig(use_nominatim=True))
        rate_limited_ctx = StageContext(
            conn=conn, config=config, out_dir=out_dir, source_dir=source_dir
        )
        mocker.patch.object(NominatimClient, "_call", return_value={"name": "Hofburg"})
        upsert_media(conn, make_media("vienna", lat=VIENNA[0], lon=VIENNA[1]))

        GeocodeStage().run(rate_limited_ctx)

        [place] = _place_rows(conn)
        assert place["source"] == "nominatim"
        assert place["poi"] == "Hofburg"
        assert place["city"] == "Vienna"


class TestUnresolvableCoordinateDegradesGracefully:
    """A mid-ocean point has no nearby city, but the k-d tree always returns *a* nearest one."""

    def test_mid_ocean_coordinate_still_gets_a_place(self, ctx, make_media) -> None:
        from story_book.db.connection import upsert_media

        upsert_media(ctx.conn, make_media("ocean", lat=MID_OCEAN[0], lon=MID_OCEAN[1]))

        GeocodeStage().run(ctx)

        media = get_media(ctx.conn, "ocean")
        assert media.place_id is not None
        [place] = _place_rows(ctx.conn)
        assert place["city"] is not None
        assert place["country"] is not None


class TestIdempotence:
    """Running the stage twice must not duplicate places or change results."""

    def test_second_run_is_a_no_op(self, ctx, make_media) -> None:
        from story_book.db.connection import upsert_media

        upsert_media(ctx.conn, make_media("a", lat=VIENNA[0], lon=VIENNA[1]))
        upsert_media(ctx.conn, make_media("b", lat=SALZBURG[0], lon=SALZBURG[1]))

        GeocodeStage().run(ctx)
        first_places = {row["id"]: dict(row) for row in _place_rows(ctx.conn)}
        first_place_ids = {h: get_media(ctx.conn, h).place_id for h in ("a", "b")}

        GeocodeStage().run(ctx)
        second_places = {row["id"]: dict(row) for row in _place_rows(ctx.conn)}
        second_place_ids = {h: get_media(ctx.conn, h).place_id for h in ("a", "b")}

        assert first_places == second_places
        assert first_place_ids == second_place_ids


class TestAvailability:
    def test_available_when_extra_installed(self, ctx) -> None:
        ok, reason = GeocodeStage().available(ctx)

        assert ok is True
        assert reason == ""


class TestPlaceIdentityIsResolvedContentNotCoordinate:
    """A place is "Vienna", not "the square metre where we looked Vienna up".

    Keying row identity on the rounded coordinate produced 159 rows all saying "Vienna" for one
    real 286-item trip, because the default rounding is ~11 m. That is not just wasteful:
    `event.place_id` points at these rows, so two events in the same square would carry different
    place ids for the same city and any grouping or labelling by place would fragment.
    """

    def _seed_nearby(self, ctx: StageContext, make_media, count: int) -> None:
        for index in range(count):
            db.upsert_media(
                ctx.conn,
                make_media(
                    f"item{index}",
                    lat=VIENNA[0] + index * 0.0002,  # ~22 m apart: distinct at 4 decimals
                    lon=VIENNA[1] + index * 0.0002,
                    gps_source=GpsSource.EXIF,
                ),
            )

    def test_many_nearby_items_collapse_to_one_place(self, ctx: StageContext, make_media) -> None:
        self._seed_nearby(ctx, make_media, 12)
        GeocodeStage().run(ctx)
        assert ctx.conn.execute("SELECT COUNT(*) AS n FROM place").fetchone()["n"] == 1

    def test_all_of_them_share_one_place_id(self, ctx: StageContext, make_media) -> None:
        self._seed_nearby(ctx, make_media, 12)
        GeocodeStage().run(ctx)
        ids = {m.place_id for m in db.iter_media(ctx.conn)}
        assert len(ids) == 1

    def test_genuinely_different_cities_stay_separate(self, ctx: StageContext, make_media) -> None:
        db.upsert_media(
            ctx.conn,
            make_media("vienna", lat=VIENNA[0], lon=VIENNA[1], gps_source=GpsSource.EXIF),
        )
        db.upsert_media(
            ctx.conn,
            make_media("istanbul", lat=41.0082, lon=28.9784, gps_source=GpsSource.EXIF),
        )
        GeocodeStage().run(ctx)
        assert ctx.conn.execute("SELECT COUNT(*) AS n FROM place").fetchone()["n"] == 2

    def test_a_rerun_does_not_add_rows(self, ctx: StageContext, make_media) -> None:
        self._seed_nearby(ctx, make_media, 8)
        GeocodeStage().run(ctx)
        GeocodeStage().run(ctx)
        assert ctx.conn.execute("SELECT COUNT(*) AS n FROM place").fetchone()["n"] == 1

    def test_stale_coordinate_keyed_rows_are_collapsed_on_rerun(
        self, ctx: StageContext, make_media
    ) -> None:
        """Forcing a stage must give the same answer as a fresh build.

        Simulates a database written under the old coordinate-keyed identity: many rows, identical
        content, each owning some media.
        """
        self._seed_nearby(ctx, make_media, 6)
        for index, media in enumerate(db.iter_media(ctx.conn)):
            cursor = ctx.conn.execute(
                """
                INSERT INTO place (lat_key, lon_key, poi, city, region, country, source)
                VALUES (?, ?, NULL, 'Vienna', 'Vienna', 'AT', 'offline')
                """,
                (VIENNA[0] + index * 0.0002, VIENNA[1] + index * 0.0002),
            )
            media.place_id = cursor.lastrowid
            db.upsert_media(ctx.conn, media)
        assert ctx.conn.execute("SELECT COUNT(*) AS n FROM place").fetchone()["n"] == 6

        GeocodeStage().run(ctx)

        assert ctx.conn.execute("SELECT COUNT(*) AS n FROM place").fetchone()["n"] == 1

    def test_a_place_referenced_by_an_event_is_never_deleted(
        self, ctx: StageContext, make_media
    ) -> None:
        """Orphan cleanup must not cascade into real downstream work."""
        cursor = ctx.conn.execute(
            """
            INSERT INTO place (lat_key, lon_key, poi, city, region, country, source)
            VALUES (9.0, 9.0, NULL, 'Elsewhere', 'R', 'XX', 'offline')
            """
        )
        place_id = cursor.lastrowid
        ctx.conn.execute("INSERT INTO day (trip_id, local_date) VALUES (1, '2026-07-18')")
        day_id = ctx.conn.execute("SELECT id FROM day").fetchone()["id"]
        ctx.conn.execute(
            "INSERT INTO event (day_id, seq, place_id) VALUES (?, 1, ?)", (day_id, place_id)
        )

        GeocodeStage().run(ctx)

        rows = ctx.conn.execute(
            "SELECT COUNT(*) AS n FROM place WHERE id = ?", (place_id,)
        ).fetchone()
        assert rows["n"] == 1
