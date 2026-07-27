"""Pure report helpers: formatting, projection, and the SVG map. No filesystem."""

from __future__ import annotations

from story_book.export.report import (
    MAP_HEIGHT,
    MAP_WIDTH,
    _project,
    clock,
    country_name,
    duration,
    place_label,
    render_map,
)

VIENNA = (48.2082, 16.3738)


class TestCountryName:
    def test_a_known_code_becomes_a_name(self):
        assert country_name("AT") == "Austria"

    def test_a_lowercase_code_still_resolves(self):
        assert country_name("at") == "Austria"

    def test_an_unknown_code_passes_through_rather_than_being_guessed_at(self):
        assert country_name("ZZ") == "ZZ"

    def test_no_code_is_no_name(self):
        assert country_name(None) is None


class TestPlaceLabel:
    def test_it_joins_poi_city_and_country(self):
        place = {"poi": "Stephansplatz", "city": "Vienna", "country": "AT"}
        assert place_label(place) == "Stephansplatz, Vienna, Austria"

    def test_missing_parts_are_dropped_not_rendered_as_none(self):
        assert place_label({"poi": None, "city": "Vienna", "country": "AT"}) == "Vienna, Austria"

    def test_no_place_is_an_empty_string(self):
        assert place_label(None) == ""


class TestClock:
    def test_it_takes_the_time_from_an_isoformat_stamp(self):
        assert clock("2026-07-18T15:46:12") == "15:46"

    def test_a_missing_timestamp_renders_empty_not_none(self):
        assert clock(None) == ""

    def test_a_short_string_is_not_sliced_into_nonsense(self):
        assert clock("2026-07-18") == ""


class TestDuration:
    def test_a_short_stop_stays_in_minutes(self):
        assert duration(45) == "45 min"

    def test_a_long_stop_reads_in_hours(self):
        assert duration(525) == "8h 45m"

    def test_minutes_are_zero_padded(self):
        assert duration(305) == "5h 05m"

    def test_no_duration_renders_empty(self):
        assert duration(None) == ""


class TestProject:
    def test_no_points_projects_to_nothing(self):
        assert _project([]) == []

    def test_points_land_inside_the_viewport(self):
        points = [[48.20, 16.36], [48.22, 16.40], [48.21, 16.37]]
        for x, y in _project(points):
            assert 0 <= x <= MAP_WIDTH
            assert 0 <= y <= MAP_HEIGHT

    def test_north_is_up(self):
        (_, y_south), (_, y_north) = _project([[48.20, 16.37], [48.22, 16.37]])
        assert y_north < y_south

    def test_a_single_point_does_not_divide_by_zero(self):
        assert len(_project([list(VIENNA)])) == 1

    def test_two_identical_points_do_not_divide_by_zero(self):
        assert len(_project([list(VIENNA), list(VIENNA)])) == 2

    def test_a_wide_route_uses_most_of_the_canvas(self):
        """Fitting the wider span into the shorter side left a day's route in a third of the box."""
        points = [[48.21, 16.33], [48.215, 16.42]]
        xs = [x for x, _ in _project(points)]
        assert max(xs) - min(xs) > MAP_WIDTH * 0.6


class TestRenderMap:
    def test_no_location_data_says_so_rather_than_drawing_an_empty_box(self):
        assert "No location data" in render_map([], [])

    def test_a_mark_becomes_a_dot(self):
        svg = render_map([], [{"lat": 48.2, "lon": 16.3, "interpolated": False, "label": "x"}])
        assert "<circle" in svg

    def test_an_interpolated_fix_is_drawn_differently(self):
        """Success criterion 7 -- the interpolated points are the ones the map might be lying
        about, so a reader has to be able to tell."""
        measured = render_map([], [{"lat": 48.2, "lon": 16.3, "interpolated": False, "label": "a"}])
        guessed = render_map([], [{"lat": 48.2, "lon": 16.3, "interpolated": True, "label": "a"}])
        assert 'class="dot"' in measured
        assert 'class="dot interpolated"' in guessed

    def test_a_route_of_two_or_more_points_draws_a_path(self):
        assert "<path" in render_map([[48.20, 16.36], [48.22, 16.40]], [])

    def test_a_single_route_point_draws_no_path(self):
        assert "<path" not in render_map([[48.20, 16.36]], [])

    def test_a_label_is_escaped_into_the_tooltip(self):
        svg = render_map([], [{"lat": 48.2, "lon": 16.3, "interpolated": False, "label": "a<b>&c"}])
        assert "<b>" not in svg
        assert "&lt;b&gt;" in svg
