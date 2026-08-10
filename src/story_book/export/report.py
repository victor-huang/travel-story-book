"""Module 13: the static HTML report.

A pure function of `trip.json` plus the derived images beside it. Nothing here touches the
database, and nothing in the output is a source of truth -- delete the directory and re-render.
That is what makes the edit-`overrides.toml`-and-re-run loop practical.

**The map is Leaflet over OpenStreetMap tiles, with the SVG kept as a fallback.** This reverses an
earlier decision. The report must be browsable offline, so the first version drew an inline SVG and
argued that "a map whose tiles are grey rectangles is worse than no map". Seen in a finished book
that was overstated: Leaflet still draws the route and markers when tiles fail, so offline degrades
to roughly the SVG rather than to nothing, while online is a real street map -- which is what a
reader wants when trying to remember where they walked.

Leaflet's JS and CSS are **vendored**, so nothing is fetched from a CDN and the page has no
external code dependency. Only tile images cross the network, and only when there is one. The SVG
lives inside a `<noscript>`, so a browser with JavaScript disabled still sees the route.

This adds JavaScript but no JavaScript *state*, which is what the plan's constraint was about:
nothing is stored, no history is pushed, the back button behaves.

Page structure lives in `templates/` so the look can be changed without touching Python. What
stays here is the part that is genuinely computation: projecting coordinates, and turning the
document's raw fields into the handful of strings a template should not be deriving itself.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

logger = logging.getLogger(__name__)

REPORT_DIRNAME = "report"
TEMPLATE_DIR = Path(__file__).parent / "templates"
VENDOR_DIR = Path(__file__).parent / "vendor"
STYLESHEET_NAME = "style.css"

TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
MAX_ZOOM = 18
MAP_SCRIPT = TEMPLATE_DIR / "_map.js"

# Derived images live beside the report directory, not inside it, because `build` writes them
# and `report` only reads them -- and `render_report` deletes its own directory wholesale on every
# run. So media references climb out of `report/` first. Getting this wrong broke every image on
# the page while the HTML validated perfectly, which is why a test now resolves each src to a
# real file.
MEDIA_REL_FROM_INDEX = "../"
MEDIA_REL_FROM_DAY = "../../"


@dataclass(frozen=True, slots=True)
class MediaPrefix:
    """What every image reference in the report is prefixed with.

    Two prefixes rather than one because the index and the day pages sit at different depths.

    The default is the relative pair above and is what `build` and `report` always use. The iOS
    app renders the *same* report with a custom scheme -- `MediaPrefix.absolute("storyasset://")`
    -- and resolves each request against the phone's originals, falling back to the server preview
    (iOS tracker D4/I25). That is a parameter here rather than string surgery on generated HTML in
    the app, because an adapter rewriting someone else's markup is unbounded work and a second
    renderer would drift from this one within a release.
    """

    from_index: str = MEDIA_REL_FROM_INDEX
    from_day: str = MEDIA_REL_FROM_DAY

    @classmethod
    def absolute(cls, prefix: str) -> MediaPrefix:
        """One prefix for both depths -- correct only when it is depth-independent.

        A scheme like `storyasset://` addresses the asset directly, so `../` would be nonsense.
        Passing a *relative* prefix here would silently break the day pages, which sit one level
        deeper than the index.
        """
        return cls(from_index=prefix, from_day=prefix)


MAP_WIDTH = 720
MAP_HEIGHT = 420
MAP_PADDING = 28
MIN_SPAN_DEGREES = 0.002
"""Below this the day happened in one spot; the viewport widens so the map is not a single dot."""


@dataclass(frozen=True, slots=True)
class RenderedReport:
    root: Path
    index: Path
    day_pages: tuple[Path, ...]

    @property
    def page_count(self) -> int:
        return 1 + len(self.day_pages)


# ISO alpha-2 codes the offline geocoder returns. `place.country` stores `AT`, and the brief and
# the report both want "Austria" -- a presentation concern, which is why the mapping lives here
# and not in the geocoder. An unknown code is returned unchanged rather than guessed at.
_COUNTRY_NAMES = {
    "AT": "Austria",
    "BE": "Belgium",
    "CA": "Canada",
    "CH": "Switzerland",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "GR": "Greece",
    "HR": "Croatia",
    "HU": "Hungary",
    "IE": "Ireland",
    "IS": "Iceland",
    "IT": "Italy",
    "JP": "Japan",
    "KR": "South Korea",
    "MX": "Mexico",
    "NL": "Netherlands",
    "NO": "Norway",
    "NZ": "New Zealand",
    "PL": "Poland",
    "PT": "Portugal",
    "SE": "Sweden",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "TW": "Taiwan",
    "US": "United States",
}


def country_name(code: str | None) -> str | None:
    """`AT` -> `Austria`. An unrecognised code passes through: unhelpful but truthful."""
    if not code:
        return None
    return _COUNTRY_NAMES.get(code.upper(), code)


def place_label(place: dict | None) -> str:
    if not place:
        return ""
    parts = [place.get("poi"), place.get("city"), country_name(place.get("country"))]
    return ", ".join(p for p in parts if p)


def clip_length(seconds: float | None) -> str:
    """`0.37` -> `<1s`, not `0s`. Zero reads as missing data rather than a very short clip."""
    if not seconds:
        return ""
    return "<1s" if seconds < 1 else f"{round(seconds)}s"


def clock(value: str | None) -> str:
    """`2026-07-18T15:46:12` -> `15:46`. Empty for a missing timestamp, never the word None."""
    return value[11:16] if value and len(value) >= 16 else ""


def _project(points: list[list[float]]) -> list[tuple[float, float]]:
    """Lat/lon to SVG coordinates: north up, and longitude scaled by latitude.

    Without the cosine term a day's walk is visibly stretched east-west at Vienna's latitude,
    which makes an out-and-back look like a loop.
    """
    if not points:
        return []
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat_mid = (min(lats) + max(lats)) / 2
    shrink = math.cos(math.radians(lat_mid))
    lat_span = max(max(lats) - min(lats), MIN_SPAN_DEGREES)
    lon_span = max((max(lons) - min(lons)) * shrink, MIN_SPAN_DEGREES)
    lat_c, lon_c = lat_mid, (min(lons) + max(lons)) / 2
    # One scale for both axes, so the route keeps its true shape -- but chosen against each
    # axis's own available space rather than fitting the wider span into the shorter side, which
    # left a wide day's route occupying a third of the canvas.
    scale = min(
        (MAP_WIDTH - 2 * MAP_PADDING) / lon_span,
        (MAP_HEIGHT - 2 * MAP_PADDING) / lat_span,
    )
    return [
        (
            MAP_WIDTH / 2 + (lon - lon_c) * shrink * scale,
            MAP_HEIGHT / 2 - (lat - lat_c) * scale,
        )
        for lat, lon in points
    ]


def render_map(path: list[list[float]], marks: list[dict]) -> Markup:
    """The day as SVG: the route, plus a dot per item.

    Interpolated positions are drawn hollow. That is success criterion 7, and it matters because
    the interpolated points are exactly the ones the map might be lying about.
    """
    if not marks and not path:
        return Markup('<p class="empty">No location data for this day.</p>')

    projected = _project([[m["lat"], m["lon"]] for m in marks] + [list(p) for p in path])
    mark_points, path_points = projected[: len(marks)], projected[len(marks) :]

    parts = [
        f'<svg class="map" viewBox="0 0 {MAP_WIDTH} {MAP_HEIGHT}" role="img" '
        f'aria-label="Route for the day">',
        f'<rect width="{MAP_WIDTH}" height="{MAP_HEIGHT}" class="map-bg"/>',
    ]
    if len(path_points) >= 2:
        d = " ".join(
            f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(path_points)
        )
        parts.append(f'<path d="{d}" class="route"/>')
    for (x, y), mark in zip(mark_points, marks, strict=True):
        css = "dot interpolated" if mark["interpolated"] else "dot"
        label = Markup.escape(mark["label"])
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" class="{css}"><title>{label}</title></circle>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def map_data(path: list[list[float]], marks: list[dict]) -> Markup:
    """The route and markers as JSON for Leaflet.

    Emitted into a `<script type="application/json">` block rather than interpolated into
    executable code: the labels are photo filenames, and a filename should never be able to
    become script. `</` is escaped so the payload cannot close its own tag either.
    """
    payload = {
        "route": [[round(lat, 6), round(lon, 6)] for lat, lon in path],
        "marks": [
            {
                "lat": round(m["lat"], 6),
                "lon": round(m["lon"], 6),
                "interpolated": bool(m["interpolated"]),
                "label": m["label"],
            }
            for m in marks
        ],
        "tileUrl": TILE_URL,
        "attribution": TILE_ATTRIBUTION,
        "maxZoom": MAX_ZOOM,
    }
    return Markup(json.dumps(payload).replace("</", "<\\/"))


def _osm_url(points: list[list[float]]) -> str | None:
    if not points:
        return None
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    return f"https://www.openstreetmap.org/#map=15/{lat:.5f}/{lon:.5f}"


def _notes(doc: dict) -> list[Markup]:
    """What did *not* happen. A filter that never ran must not read as a clean result."""
    notes: list[Markup] = []
    privacy = doc["privacy"]
    if not privacy["home_configured"]:
        notes.append(
            Markup(
                "No <code>home</code> is configured, so the privacy filter did not run — "
                "no media was checked against a home location."
            )
        )
    elif privacy["excluded_near_home"]:
        notes.append(
            Markup(f"{privacy['excluded_near_home']} item(s) near home are kept out of exports.")
        )
    if doc["trip"]["counts"]["undated"]:
        notes.append(
            Markup(
                f"{doc['trip']['counts']['undated']} item(s) have no usable timestamp and "
                "appear on no day."
            )
        )
    if not doc["context"]["supplied"]:
        notes.append(
            Markup(
                "No trip context was supplied, so any journal written from this will stay "
                "factual — see <code>trip_context.example.toml</code>."
            )
        )
    return notes


def duration(minutes: float | None) -> str:
    """`525` -> `8h 45m`. Nobody reads a travel report in minutes past 90."""
    if minutes is None:
        return ""
    total = int(round(minutes))
    if total < 90:
        return f"{total} min"
    return f"{total // 60}h {total % 60:02d}m"


def _event_facts(event: dict) -> list[str]:
    location = event["location"]
    facts = []
    if event["duration_minutes"] is not None:
        facts.append(duration(event["duration_minutes"]))
    facts.append(f"{event['counts']['media']} items")
    if location["radius_m"]:
        facts.append(f"within {location['radius_m']:.0f} m")
    if location["gps_coverage"] < 1.0:
        facts.append(f"{location['gps_coverage']:.0%} located")
    return facts


def _story_for(story: dict | None, date: str) -> dict[str, Any]:
    """The written material for one day, indexed by the ids it cites.

    The report stays a pure function of `trip.json` plus this: a story is an overlay, never a
    source of structure. Chapters group photos the pipeline already knows about, so an unknown
    id is dropped here rather than inventing a page -- `check-story` is where that gets reported.
    """
    if not story:
        # Every key the template touches, present and empty. StrictUndefined turns a key that
        # exists in one branch and not the other into a failed render, which is the right
        # trade -- but the two branches have to agree.
        return {
            "narrative": None,
            "title": None,
            "summary": None,
            "chapters": [],
            "captions": {},
            "uncertainties": [],
        }
    captions = {
        c["asset_id"]: c["caption"]
        for c in story.get("captions", []) or []
        if isinstance(c, dict) and c.get("asset_id")
    }
    day_entry = next((d for d in story.get("days", []) or [] if d.get("date") == date), {})
    return {
        # `narrative` is what the contract asks for; `summary` is what a real response used
        # instead. `check-story` is strict about the shape, and the renderer is generous with
        # what it got -- throwing away four good paragraphs over a key name helps nobody.
        "narrative": day_entry.get("narrative") or day_entry.get("summary"),
        "title": day_entry.get("title"),
        "summary": day_entry.get("summary"),
        "chapters": [c for c in story.get("chapters", []) or [] if c.get("date") == date],
        "captions": captions,
        "uncertainties": _day_uncertainties(story, date),
    }


def _day_uncertainties(story: dict, date: str) -> list[str]:
    """Every hedge the writer attached to this day, surfaced rather than buried.

    An uncertainty the reader never sees is the same as no uncertainty at all, and these are
    exactly the claims that need a human's eye -- a landmark named from a photograph, an
    attraction inferred from conversation.
    """
    out: list[str] = []
    for chapter in story.get("chapters", []) or []:
        if chapter.get("date") == date:
            out += [str(u) for u in chapter.get("uncertainties", []) or []]
    for item in story.get("uncertainties", []) or story.get("global_uncertainties", []) or []:
        text = item if isinstance(item, str) else json.dumps(item)
        if date in text or (isinstance(item, dict) and date[5:] in str(item.get("topic", ""))):
            out.append(text)
    return out


def _day_view(doc: dict, day: dict, story: dict | None = None) -> dict[str, Any]:
    """Everything a day page needs, resolved from asset ids to asset records."""
    assets = doc["assets"]
    events = []
    all_assets = []
    marks = []
    for event in day["events"]:
        members = [assets[a] for a in event["assets"] if a in assets]
        all_assets.extend(members)
        for asset in members:
            location = asset.get("location")
            if location:
                marks.append(
                    {
                        "lat": location["lat"],
                        "lon": location["lon"],
                        "interpolated": location["source"] == "interpolated",
                        "label": f"{clock(asset['taken_local'])} {asset['filename']}",
                    }
                )
        # The day's *chosen* highlights, grouped under the stop they came from -- not the
        # event-scope selection, which exists to sample landmark recognition cheaply and is
        # capped at a few per event. Showing the sample here buried the 24 photos a reader
        # actually wants under a 141-item gallery.
        chosen = [assets[a] for a in day["highlights"] if a in assets and a in set(event["assets"])]
        events.append(
            {
                **event,
                "title": event["label"] or place_label(event["place"]) or "Unnamed stop",
                "facts": _event_facts(event),
                "highlight_assets": chosen
                or [assets[a] for a in event["highlights"] if a in assets],
            }
        )
    written = _story_for(story, day["date"])
    by_id = {a["asset_id"]: a for a in all_assets}
    chapters = [
        {
            **chapter,
            "assets": [by_id[a] for a in chapter.get("asset_ids", []) if a in by_id],
        }
        for chapter in written["chapters"]
    ]
    return {
        **day,
        "events": events,
        "all_assets": all_assets,
        "marks": marks,
        "story": {**written, "chapters": chapters},
    }


def _index_view(doc: dict) -> list[dict[str, Any]]:
    assets = doc["assets"]
    view = []
    for day in doc["days"]:
        cover = next(
            (assets[a] for a in day["highlights"] if a in assets and assets[a].get("thumbnail")),
            None,
        )
        places = sorted({place_label(e["place"]) for e in day["events"] if place_label(e["place"])})
        view.append({**day, "cover": cover, "places": places})
    return view


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["clock"] = clock
    env.filters["duration"] = duration
    env.filters["clip_length"] = clip_length
    env.filters["place_label"] = place_label
    env.filters["country_name"] = country_name
    return env


def render_report(
    doc: dict,
    out_dir: Path,
    story: dict | None = None,
    media_prefix: MediaPrefix | None = None,
) -> RenderedReport:
    """Write `index.html`, a page per day, and the stylesheet into `<out_dir>/report/`.

    The directory is rebuilt from scratch each time. Nothing in it is a source of truth, and a
    day page left behind from a previous run would be a correction that failed to take.
    """
    env = _environment()
    prefix = media_prefix or MediaPrefix()
    root = out_dir / REPORT_DIRNAME
    if root.exists():
        shutil.rmtree(root)
    (root / "days").mkdir(parents=True)
    shutil.copyfile(TEMPLATE_DIR / STYLESHEET_NAME, root / STYLESHEET_NAME)
    # Vendored, not linked: a CDN reference makes the report depend on someone else's uptime and
    # leaks a request every time it is opened. Tiles cross the network; code does not.
    shutil.copytree(VENDOR_DIR, root / "vendor")

    captions = {
        c["asset_id"]: c["caption"]
        for c in (story or {}).get("captions", []) or []
        if isinstance(c, dict) and c.get("asset_id")
    }
    trip_highlights = [doc["assets"][a] for a in doc["trip_highlights"] if a in doc["assets"]]
    index = root / "index.html"
    index.write_text(
        env.get_template("index.html").render(
            rel="",
            media_rel=prefix.from_index,
            trip=doc["trip"],
            days=_index_view(doc),
            trip_highlights=trip_highlights,
            notes=_notes(doc),
            captions=captions,
            story=story,
        )
    )

    dates = [d["date"] for d in doc["days"]]
    pages = []
    for position, day in enumerate(doc["days"]):
        view = _day_view(doc, day, story)
        page = root / "days" / f"{day['date']}.html"
        page.write_text(
            env.get_template("day.html").render(
                rel="../",
                media_rel=prefix.from_day,
                trip=doc["trip"],
                day=view,
                marks=view["marks"],
                interpolated_count=sum(1 for m in view["marks"] if m["interpolated"]),
                map_svg=render_map(day["path"], view["marks"]),
                map_json=map_data(day["path"], view["marks"]),
                map_script=Markup(MAP_SCRIPT.read_text()),
                osm_url=_osm_url(day["path"] or [[m["lat"], m["lon"]] for m in view["marks"]]),
                captions=view["story"]["captions"],
                prev_day=dates[position - 1] if position else None,
                next_day=dates[position + 1] if position + 1 < len(dates) else None,
            )
        )
        pages.append(page)

    logger.info("report: %d page(s) in %s", 1 + len(pages), root)
    return RenderedReport(root=root, index=index, day_pages=tuple(pages))


def load_trip_json(path: Path) -> dict:
    return json.loads(path.read_text())


def load_story(path: Path | None) -> dict | None:
    """A model's `story.json`, or `None`. Never a source of structure -- only of words."""
    if path is None:
        return None
    return json.loads(path.read_text())
