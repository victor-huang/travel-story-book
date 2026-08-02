"""Module 14: the ChatGPT upload package.

The original plan was "zip each day and upload". That does not work: ChatGPT does not run vision
over images inside an archive, and the attachment limit is far below a day's photo count, so a zip
produces a journal written from filenames. What works is a small number of *labeled contact
sheets* the model can actually look at, plus a brief that refers to cells by a stable id.

P02 hand-tested the format on one real day and it produced a journal worth keeping. It also found
seven gaps, and this module exists in the shape it does because of them:

1. **`manifest.json` is authoritative and `brief.md` is generated from it.** Contact-sheet cell
   ids are positional -- `03-07` means a different photo the moment selection changes -- so the
   manifest keys everything by `asset_id` and records the cell as an *attribute*. Writing the
   brief from the manifest rather than alongside it is what keeps the two from drifting.
2. **Videos get records including explicit negatives.** `transcript_status` distinguishes
   `no_speech` from `not_processed`; a storyboard built on the assumption that silence means
   unexamined is a storyboard built on a guess.
3. **Places are named, not coordinates.** Asking a model to resolve a lat/lon invites it to name
   the place from the picture instead, confidently and sometimes wrongly.
4. **Trip context, or an explicit statement that there is none** -- so the model stays factual
   rather than inventing feelings nobody described.
5. **Structured output is requested alongside the prose**, so editorial decisions can drive a
   renderer later instead of being trapped in paragraphs.
6. **Per-event location is described** -- centroid, extent, coverage -- not averaged to a point.
7. **Component quality scores**, so "why did this photo win" is answerable. No aesthetic or
   composition score: the pipeline does not compute those.

The package also states whether it ships **previews or originals**. A preview cannot support a
judgement about focus, blink, noise, or crop headroom, and the recipient should be told rather
than left to infer it from the pixels.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from story_book.export.contact_sheet import render_contact_sheets, save_contact_sheets
from story_book.export.report import clock, country_name, duration, place_label

logger = logging.getLogger(__name__)

PACKAGE_DIRNAME = "package"
SHEETS_DIRNAME = "contact_sheets"
KEYFRAMES_DIRNAME = "keyframes"
SCHEMA_DIRNAME = "schema"
PROXIES_DIRNAME = "video_proxies"
MANIFEST_SCHEMA_FILENAME = "manifest.schema.json"
STORY_SCHEMA_FILENAME = "story.schema.json"
SCHEMA_SOURCE = Path(__file__).parent / "manifest_schema.json"
STORY_SCHEMA_SOURCE = Path(__file__).parent / "story_schema.json"

# Finder and macOS archive tooling scatter these through a zip. Harmless, but they make a package
# look unfinished and they are noise for whoever opens it.
JUNK_NAMES = (".DS_Store", "Thumbs.db", "__MACOSX", ".Spotlight-V100", ".fseventsd")
MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 3

CELLS_PER_SHEET = 12
SHEET_COLUMNS = 4
SHEET_WIDTH = 1600

PREVIEW = "preview"
ORIGINALS = "originals"


@dataclass(frozen=True, slots=True)
class PackagedDay:
    date: str
    directory: Path
    sheets: tuple[Path, ...]
    brief: Path
    prompt: Path
    asset_count: int


@dataclass(frozen=True, slots=True)
class Package:
    root: Path
    manifest: Path
    days: tuple[PackagedDay, ...]
    mode: str
    skipped: tuple[tuple[str, str], ...]
    """`(filename, reason)` for anything that could not be rendered onto a sheet."""


def _link_or_copy(source: Path, target: Path) -> str:
    """Hardlink if the filesystem allows it, else copy. Never move.

    The non-destructive guarantee is the whole product: a move would take the user's original
    out of their own library. A hardlink costs no disk and cannot lose data; a cross-device link
    raises `OSError`, and then a copy is correct.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return "existing"
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        shutil.copy2(source, target)
        return "copy"


def _asset_caption(asset: dict) -> str:
    """What is drawn under a contact-sheet cell: the time, then the `asset_id`.

    The `asset_id` goes on the cell itself rather than only in the brief. The model is asked to
    reference photos by that id, and printing it here removes a lookup -- it can cite what it can
    see. The place name is deliberately *not* here: on a single-city day it repeated "Vienna"
    twelve times per sheet and pushed the useful part out of the caption's width.
    """
    bits = [clock(asset["taken_local"]), asset["asset_id"]]
    if asset["kind"] == "video":
        bits.append(f"video {_seconds(asset)}")
    return "  ".join(b for b in bits if b)


def _quality_summary(asset: dict) -> dict[str, Any] | None:
    quality = asset.get("quality")
    if not quality:
        return None
    return {
        "overall": _round(quality["overall"]),
        "sharpness": _round(quality["sharpness"]),
        "exposure": _round(quality["exposure"]),
        "contrast": _round(quality["contrast"]),
        "faces_detected": quality["face_count"],
        "content_class": quality["content_class"],
    }


def _round(value: float | None, places: int = 3) -> float | None:
    return None if value is None else round(value, places)


def _video_summary(asset: dict) -> dict[str, Any] | None:
    video = asset.get("video")
    if not video:
        return None
    transcript = video.get("transcript")
    keyframes = video.get("keyframes") or []
    return {
        "duration_seconds": _round(video["duration_seconds"], 1),
        "subtype": video["subtype"],
        # A 0.37-second clip is not storyboard material. Marked rather than dropped: a human who
        # pinned it still gets it, and silently excluding footage is its own kind of lie.
        "storyboard_candidate": video["storyboard_candidate"],
        "fps": _round(video.get("fps"), 2),
        "motion_score": _round(video["motion_score"]),
        # Frames *and* their offsets into the clip. Without them a storyboard can say how long to
        # use a shot but not which part of it, and one contact-sheet cell cannot stand in for a
        # 112-second video.
        "keyframes": [{"seconds": frame["seconds"], "preview_path": None} for frame in keyframes],
        # The distinction P02 asked for. `no_speech` is a measured negative; `not_processed`
        # means nobody listened, and a storyboard must not treat the two the same way.
        "transcript_status": video["transcript_status"],
        "transcript_text": (transcript or {}).get("text"),
    }


def _selection_reasons(asset: dict) -> list[str]:
    """Why this asset is in the package, in the caller's terms rather than the pipeline's.

    A model choosing a hero image benefits from knowing that a photo is here because a human asked
    for it, versus because it scored well, versus because every clip is exported for the
    storyboard whether or not it won a slot.
    """
    selected = asset.get("selected", {})
    reasons = []
    if selected.get("day", {}).get("reason") == "pinned":
        reasons.append("human_pinned")
    if "day" in selected and "human_pinned" not in reasons:
        reasons.append("quality_ranked")
    if "trip" in selected:
        reasons.append("trip_highlight")
    if "event" in selected:
        reasons.append("event_representative")
    quality = asset.get("quality") or {}
    if (quality.get("face_count") or 0) > 0:
        reasons.append("faces_present")
    if asset["kind"] == "video" and not reasons:
        reasons.append("all_video_exported")
    cluster = asset.get("cluster")
    if cluster and cluster.get("is_keeper"):
        reasons.append("duplicate_group_keeper")
    return reasons


def _export_kind(asset: dict, proxies: bool) -> dict[str, Any]:
    """What the exported file for this asset actually *is*.

    The bug this exists to prevent: a video's exported preview was its poster frame -- a JPEG --
    written under the source's own `.mov` name, so the package advertised nine playable clips and
    shipped nine still images with a lying extension. A consumer decoding them fails, and one that
    trusts the manifest believes it has footage it does not have.
    """
    if asset["kind"] != "video":
        return {
            "source_media_type": "image/jpeg",
            "export_media_type": "image/jpeg",
            "export_role": "preview_image",
            "video_proxy_included": False,
        }
    return {
        "source_media_type": "video/quicktime",
        "export_media_type": "video/mp4" if proxies else "image/jpeg",
        "export_role": "video_proxy" if proxies else "poster_frame",
        "video_proxy_included": proxies,
    }


def _asset_record(
    asset: dict, day: str, event_id: str | None, export_path: str | None, proxies: bool = False
) -> dict:
    location = asset.get("location")
    place = (location or {}).get("place")
    selected = asset.get("selected", {})
    return {
        "asset_id": asset["asset_id"],
        "content_hash": asset["content_hash"],
        "source_filename": asset["filename"],
        "kind": asset["kind"],
        "day": day,
        "calendar_date": asset["calendar_date"],
        "event_id": event_id,
        "taken_local": asset["taken_local"],
        "taken_utc": asset["taken_utc"],
        "timezone": asset["timezone"]["name"],
        "export_path": export_path,
        **_export_kind(asset, proxies),
        "cell_id": None,
        **asset["geometry"],
        "place": (
            {
                "name": place_label(place),
                "city": place.get("city"),
                "country": country_name(place.get("country")),
                # The source, and no confidence number. The offline geocoder returns a nearest
                # populated place from a bundled dataset and reports no confidence; inventing one
                # would be a fabricated measurement, which is the failure this project keeps
                # guarding against. City-level precision is stated instead.
                "source": place.get("source"),
                "precision": "city",
            }
            if place
            else None
        ),
        "quality": _quality_summary(asset),
        "video": _video_summary(asset),
        # `pinned_by_human` lives here and nowhere else. It was previously duplicated at the top
        # level of the record, which is two places for one fact and one of them eventually wrong.
        "selection": {
            "included": True,
            "reasons": _selection_reasons(asset),
            "rank_within_day": selected.get("day", {}).get("rank"),
            "pinned_by_human": selected.get("day", {}).get("reason") == "pinned",
        },
    }


def _event_record(event: dict, asset_ids: list[str]) -> dict:
    location = event["location"]
    return {
        "event_id": event["id"],
        # Detected time-and-location cluster, *not* a narrative chapter. One real cluster here runs
        # 8h45m over 129 items -- geographically coherent and far too broad to be a chapter.
        # Chapters are the model's job, and the prompt asks for them with `source_event_ids`.
        "event_type": "detected_cluster",
        "label": event["label"],
        "place": place_label(event["place"]) or None,
        "start_local": event["start_local"],
        "end_local": event["end_local"],
        "duration_seconds": event["duration_seconds"],
        "duration_display": duration(event["duration_minutes"]),
        "counts": event["counts"],
        # Centroid *and* extent: one averaged coordinate can place an event somewhere nobody
        # stood, and hides whether the hour was spent walking or sitting.
        "location": {
            "centroid": location["centroid"],
            "start": location["first"],
            "end": location["last"],
            "radius_m": location["radius_m"],
            "gps_coverage": location["gps_coverage"],
            "moved": location["path"] is not None,
        },
        "landmarks": [landmark["name"] for landmark in event["landmarks"]],
        "asset_ids": asset_ids,
    }


def build_manifest(doc: dict, mode: str, proxies: bool = False) -> dict[str, Any]:
    """The authoritative artifact. Everything else in the package derives from this."""
    assets = doc["assets"]
    days = []
    for day in doc["days"]:
        chosen = [assets[a] for a in day["highlights"] if a in assets]
        # Every video comes along whether or not it won a highlight slot: P02 asked for a video
        # storyboard, and a storyboard cannot reference footage the package never mentioned.
        chosen_ids = {a["asset_id"] for a in chosen}
        videos = [
            assets[a]
            for event in day["events"]
            for a in event["assets"]
            if a in assets and assets[a]["kind"] == "video" and a not in chosen_ids
        ]
        members = sorted(chosen + videos, key=lambda a: (a["taken_local"] or "", a["asset_id"]))
        event_of = {
            asset_id: event["id"] for event in day["events"] for asset_id in event["assets"]
        }
        included = {
            "media": len(members),
            "images": sum(1 for a in members if a["kind"] == "image"),
            "videos": sum(1 for a in members if a["kind"] == "video"),
        }
        days.append(
            {
                "date": day["date"],
                "timezone": day["timezone"],
                # Two different numbers that were previously one. `assets` holds the *selected*
                # subset, so a consumer reading a single `counts.media` of 141 beside 33 records
                # could reasonably conclude the export had lost something.
                "counts": {"captured": day["counts"], "included": included},
                "asset_scope": "selected_only",
                "gps_coverage": day["gps_coverage"],
                "events": [
                    _event_record(
                        event, [a for a in event["assets"] if a in {m["asset_id"] for m in members}]
                    )
                    for event in day["events"]
                ],
                "assets": [
                    _asset_record(a, day["date"], event_of.get(a["asset_id"]), None, proxies)
                    for a in members
                ],
                "sheets": [],
            }
        )

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator": doc["generator"],
        "package": {
            "mode": mode,
            # A preview cannot support judgements about focus, blink, noise or crop headroom.
            # Saying so is cheaper than a reviewer inferring it wrongly from a soft JPEG.
            "media_note": (
                "Images are downscaled previews. Do not judge focus, blink, noise, or crop "
                "headroom from them."
                if mode == PREVIEW
                else "Full-resolution originals are included."
            ),
            "trip_json_schema_version": doc["schema_version"],
            "video_proxies_included": proxies,
            # Said plainly, because the previous package looked like it had footage and did not.
            "video_note": (
                "Playable MP4 proxies are included under each day's video_proxies/. Inspect them "
                "when choosing source ranges."
                if proxies
                else "No playable video is included. Each clip ships a poster frame and evenly "
                "sampled keyframes with their offsets; footage between keyframes is not visible, "
                "so any source range is an estimate."
            ),
        },
        "trip": {
            "name": doc["trip"]["name"],
            "start_local": doc["trip"]["start_local"],
            "end_local": doc["trip"]["end_local"],
            "timezone": doc["trip"]["timezone"],
            "day_assignment_rule": doc["trip"]["day_assignment_rule"],
            "start_utc": doc["trip"]["start_utc"],
            "end_utc": doc["trip"]["end_utc"],
            "counts": doc["trip"]["counts"],
        },
        "context": doc["context"],
        "privacy": doc["privacy"],
        "days": days,
    }


def _render_brief(manifest: dict, day: dict) -> str:
    """`brief.md`, generated *from the manifest* so the two cannot drift."""
    by_id = {a["asset_id"]: a for a in day["assets"]}
    lines = [
        f"# {day['date']} — {manifest['trip']['name'] or 'Trip'}",
        "",
        f"{day['counts']['captured']['media']} items captured "
        f"({day['counts']['captured']['images']} photos, "
        f"{day['counts']['captured']['videos']} videos) across "
        f"{day['counts']['captured']['events']} stops. "
        f"{day['counts']['included']['media']} are included in this package "
        f"({day['counts']['included']['images']} photos, "
        f"{day['counts']['included']['videos']} videos)."
        + (f" Local time is {day['timezone']}." if day["timezone"] else "")
        + f" {manifest['trip']['day_assignment_rule'].capitalize()}, so a stop after midnight "
        "carries its calendar date in brackets.",
        "",
        f"**Media in this package:** {manifest['package']['media_note']}",
        "",
        "## Stops",
        "",
    ]
    # Landmark recognition names a stop; without it every stop in one city carries the same
    # city label and the model has nothing to tell them apart. Say so rather than let three
    # identical headings imply the pipeline had nothing more to offer.
    if not any(event["landmarks"] for event in day["events"]):
        lines += [
            "> Landmark recognition did not run for this trip, so stops are named by city only. "
            "Identify them from the photographs; do not assume two stops in the same city are "
            "the same place.",
            "",
        ]

    empty = sum(1 for event in day["events"] if not event["asset_ids"])
    if empty:
        lines += [
            f"> {empty} of {len(day['events'])} stops have no photograph in this package. They are "
            "listed below so the day does not read as continuous when it was not.",
            "",
        ]

    for position, event in enumerate(day["events"], start=1):
        location = event["location"]
        header = event["label"] or event["place"] or "Unnamed stop"
        crossed = (event["start_local"] or "")[:10] not in ("", day["date"])
        when = clock(event["start_local"]) + (
            f" ({(event['start_local'] or '')[:10]})" if crossed else ""
        )
        lines.append(f"### Stop {position} · {when} · {header}")
        facts = [
            f"{clock(event['start_local'])}–{clock(event['end_local'])}",
            event["duration_display"],
            f"{event['counts']['media']} items captured",
        ]
        if location["radius_m"]:
            facts.append(
                f"{'moved through' if location['moved'] else 'stayed within'} "
                f"{location['radius_m']:.0f} m"
            )
        if location["gps_coverage"] < 1.0:
            facts.append(f"{location['gps_coverage']:.0%} of items located")
        lines += [" · ".join(f for f in facts if f), ""]
        if event["landmarks"]:
            lines += [f"Landmarks identified: {', '.join(event['landmarks'])}", ""]
        if not event["asset_ids"]:
            lines += ["*No photograph from this stop is included in the package.*", ""]
            continue
        for asset_id in event["asset_ids"]:
            asset = by_id.get(asset_id)
            if asset is None:
                continue
            lines.append(_brief_line(asset, event_place=event["place"], day_date=day["date"]))
        lines.append("")

    videos = [a for a in day["assets"] if a["kind"] == "video"]
    if videos:
        lines += ["## Video", "", manifest["package"]["video_note"], ""]
        for video in videos:
            info = video["video"]
            status = {
                "transcribed": "speech transcribed",
                "no_speech": "processed, no speech found",
                "not_processed": "not analysed for speech",
            }[info["transcript_status"]]
            marks = [_seconds(video), status]
            if info["subtype"] == "short_clip":
                marks.append("**too short for a storyboard**")
            lines.append(
                f"- `{video['asset_id']}` {_stamp(video, day['date'])} · " + " · ".join(marks)
            )
            frames = ", ".join(f"{f['seconds']:.0f}s" for f in info["keyframes"])
            if frames and info["storyboard_candidate"]:
                lines.append(f"  keyframes at {frames}")
            if info["transcript_text"]:
                lines.append(f"  > {info['transcript_text']}")
        lines.append("")

    lines += [
        "## How to refer to a photo",
        "",
        "Use the `asset_id` (the backticked code), never the cell number. Cell numbers are "
        "positional and change whenever the selection does; `asset_id` is derived from the "
        "file's content and is stable forever.",
        "",
        "| asset_id | cell | time | file | notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for asset in day["assets"]:
        notes = []
        if asset["selection"]["pinned_by_human"]:
            notes.append("chosen by the traveller")
        if asset["quality"] and asset["quality"]["faces_detected"]:
            notes.append(f"{asset['quality']['faces_detected']} face(s)")
        if asset["kind"] == "video":
            notes.append("video")
        lines.append(
            f"| `{asset['asset_id']}` | {asset['cell_id'] or '—'} | "
            f"{clock(asset['taken_local'])} | {asset['source_filename']} | "
            f"{', '.join(notes)} |"
        )
    return "\n".join(lines) + "\n"


def _seconds(asset: dict) -> str:
    """A 0.37-second clip must not read as `0s` -- that looks like missing data, not a blip."""
    value = (asset.get("video") or {}).get("duration_seconds")
    if value is None:
        return "duration unknown"
    if value < 1:
        return "<1s"
    return f"{value:.0f}s"


def _stamp(asset: dict, day_date: str) -> str:
    """`15:46`, or `00:59 (2026-07-20)` for anything shot after midnight.

    A trip day runs past midnight, so a stop can sit under 2026-07-19 while its calendar date is
    the 20th. Showing only the clock there invites a reader to sort it to the start of the day or
    conclude the timestamp is broken.
    """
    stamp = clock(asset["taken_local"])
    calendar = asset.get("calendar_date")
    if calendar and calendar != day_date:
        return f"{stamp} ({calendar})"
    return stamp


def _brief_line(asset: dict, event_place: str | None = None, day_date: str = "") -> str:
    bits = [f"- `{asset['asset_id']}`", _stamp(asset, day_date)]
    if asset["cell_id"]:
        bits.append(f"(sheet {asset['cell_id']})")
    if asset["kind"] == "video":
        bits.append(f"**video, {_seconds(asset)}**")
    # Only when it differs from the stop's own place. Repeating "Vienna, Austria" on thirty-three
    # consecutive lines is noise that buries the lines which do carry information.
    if asset["place"] and asset["place"]["name"] != event_place:
        bits.append(f"— {asset['place']['name']}")
    if asset["selection"]["pinned_by_human"]:
        bits.append("**[chosen by the traveller]**")
    if asset["quality"]:
        quality = asset["quality"]
        parts = [f"q={quality['overall']}"]
        if quality["faces_detected"]:
            parts.append(f"{quality['faces_detected']} face(s)")
        bits.append(f"({', '.join(parts)})")
    return " ".join(bits)


def _video_guidance(manifest: dict, day: dict) -> str:
    """What the model may and may not conclude about the footage.

    Asking for an exact source range when the package holds five stills from a 112-second clip
    manufactures precision: nothing between the keyframes was ever visible. Either the proxies are
    there and the range is a judgement, or they are not and it is an estimate that must say so.
    """
    videos = [a for a in day["assets"] if a["kind"] == "video"]
    if not videos:
        return "## Video\n\nThis day has no footage."
    if manifest["package"]["video_proxies_included"]:
        return (
            "## Video\n\n"
            "Playable MP4 proxies are included under `video_proxies/`. Watch them and choose "
            "`source_start_seconds` and `source_end_seconds` from what you see."
        )
    short = [a for a in videos if a["video"]["subtype"] == "short_clip"]
    note = (
        "## Video\n\n"
        "**No playable footage is included.** Each clip ships a poster frame and a few evenly "
        "sampled keyframes with their offsets in seconds. Nothing between those frames has been "
        "seen by anyone, so:\n\n"
        "- Base scene choices on the keyframes, duration, motion score and transcript status.\n"
        "- Treat `source_start_seconds` and `source_end_seconds` as **estimates**, and anchor them "
        "to a keyframe offset rather than inventing a precise moment.\n"
        "- Say in `uncertainties` which ranges you could not verify.\n"
        "- Ask for proxies in `requested_additional_context` if a scene depends on knowing exactly "
        "what happens.\n"
    )
    if short:
        ids = ", ".join(f"`{a['asset_id']}`" for a in short)
        note += (
            f"\n{len(short)} clip(s) are under two seconds ({ids}) and are marked "
            "`storyboard_candidate: false`. Skip them unless a human pinned one.\n"
        )
    return note


def _render_prompt(manifest: dict, day: dict) -> str:
    context = manifest["context"]
    if context["supplied"]:
        who = (
            ", ".join(f"{t['name'] or 'unnamed'} ({t['role']})" for t in context["travelers"])
            or "not described"
        )
        context_block = "\n".join(
            [
                "## Who this is for",
                "",
                f"- **Travellers:** {who}",
                f"- **Voice:** {context['journal_voice'] or 'not specified'}",
                *(
                    [f"- **Planned:** {plan}" for plan in context["known_plans"]]
                    if context["known_plans"]
                    else []
                ),
                *([f"- **Note:** {note}" for note in context["notes"]] if context["notes"] else []),
            ]
        )
    else:
        context_block = (
            "## Who this is for\n\n"
            "**No personal context was supplied.** Write factually about what is visible and "
            "recorded. Do not invent reactions, feelings, relationships, or reasons for the "
            "trip — an invented emotion is the same failure as an invented quote. If the "
            "absence of context limits the journal, say so in `uncertainties`."
        )

    video_guidance = _video_guidance(manifest, day)
    return f"""# Write the journal for {day["date"]}

Attached: {len(day["sheets"])} contact sheet(s) and `brief.md` for one day of a trip
({manifest["trip"]["name"] or "unnamed"}).

Each contact-sheet cell is labelled `NN-NN`. **`brief.md` maps every cell to an `asset_id`.
Refer to photos by `asset_id`, never by cell number** — cell numbers are positional and change
whenever the selection changes.

{context_block}

## What to produce

Write these as readable prose first:

1. **A journal entry** for the day, in the voice above. Ground every claim in the brief or in
   what is visible on the sheets.
2. **Captions** for the photos worth captioning, keyed by `asset_id`.
3. **A photo-book layout** — which photos share a page, which is the hero, and why.
4. **A video storyboard** using the clips listed under "Video" in the brief. Read the note there
   about what this package actually contains before you commit to a range — and note that
   `no speech found` is a *measured* result, not an unexamined clip.

{video_guidance}

## Then repeat it as JSON

After the prose, output a single fenced ```json block with this shape. **Use these key names
exactly** — a renderer consumes them, and a rename means the file cannot be read. The full contract
is `schema/story.schema.json` in this package; save your JSON as `story.json` and the traveller can
check it with `story-book check-story story.json --out <dir>`.

```json
{{
  "schema_version": 1,
  "days": [{{"date": "{day["date"]}", "narrative": "", "summary": ""}}],
  "chapters": [
    {{
      "chapter_id": "", "date": "{day["date"]}", "title": "", "narrative": "",
      "starts_at": "HH:MM",
      "source_event_ids": ["<the event_id(s) in the brief this chapter drew from — required>"],
      "asset_ids": []
    }}
  ],
  "captions": [{{"asset_id": "", "caption": ""}}],
  "layout_pages": [
    {{"page": 1, "hero_asset_id": "", "asset_ids": [], "note": ""}}
  ],
  "video_scenes": [
    {{
      "asset_id": "", "role": "",
      "source_start_seconds": 0, "source_end_seconds": 0,
      "timeline_duration_seconds": 0, "note": ""
    }}
  ],
  "uncertainties": [""],
  "requested_additional_context": [""]
}}
```

Every `asset_id` must be one that appears in the brief. `source_event_ids` is required on every
chapter: it is the only link from your editorial units back to the pipeline's own grouping, and a
renderer cannot reorganise what it cannot trace.

`uncertainties` is not optional politeness — list anything you inferred rather than read, and
anything the sheets were too small to judge. `requested_additional_context` is what you would
need to write a better entry.

## Naming places

The brief carries what the pipeline actually *resolved*, and its geocoding is city-level: a stop
named "Vienna, Austria" is confirmed to be in Vienna and nothing more. Landmark recognition may not
have run at all, in which case the brief says so.

- **Use confirmed place names directly.** Do not contradict them.
- **You may name a landmark you recognise in a photograph**, and you should if it is obvious — but
  mark it as an inference: put it in `uncertainties` as `"inferred: <name> in <asset_id> from the
  image, not from metadata"`, and use hedged language in the prose.
- **Do not invent times, durations, or place names** that appear nowhere. If something is unnamed,
  say it is unnamed.

The distinction that matters: recognising St Stephen's Cathedral in a photograph of St Stephen's
Cathedral is reading the evidence. Asserting which café the coffee came from is not.

## Chapters are yours to draw

The events in the brief are **detected time-and-location clusters**, not narrative units — one of
them may run most of a day. Group and split them however the story needs, and record which events
each chapter drew from in `source_event_ids`.
"""


def build_package(
    doc: dict,
    out_dir: Path,
    *,
    mode: str = PREVIEW,
    source_for: dict[str, Path] | None = None,
    video_proxies: bool = False,
) -> Package:
    """Write `<out_dir>/package/`: a manifest, and per day sheets, a brief, and a prompt.

    `source_for` maps `asset_id` to the file to export when `mode == "originals"`. It is passed
    in rather than read from the document because `trip.json` deliberately carries no absolute
    paths -- the package is a thing you hand to someone else.
    """
    if mode not in (PREVIEW, ORIGINALS):
        raise ValueError(f"mode must be {PREVIEW!r} or {ORIGINALS!r}, got {mode!r}")

    root = out_dir / PACKAGE_DIRNAME
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    manifest = build_manifest(doc, mode, video_proxies)
    assets = doc["assets"]
    packaged: list[PackagedDay] = []
    skipped: list[tuple[str, str]] = []

    for day in manifest["days"]:
        if not day["assets"]:
            continue
        day_dir = root / day["date"]
        media_dir = day_dir / ("full" if mode == ORIGINALS else "media")
        sheet_dir = day_dir / SHEETS_DIRNAME
        day_dir.mkdir(parents=True)

        pairs: list[tuple[Path, str]] = []
        for record in day["assets"]:
            asset = assets[record["asset_id"]]
            source = _source_path(asset, out_dir, mode, source_for)
            if source is None or not source.exists():
                skipped.append((record["source_filename"], "no exportable file"))
                continue

            if record["kind"] == "video":
                # The poster frame always goes in, under a name that says what it is. Writing a
                # JPEG under the source's `.mov` name is how the last package came to advertise
                # footage it did not contain.
                poster = media_dir / f"{record['asset_id']}_poster.jpg"
                _link_or_copy(source, poster)
                pairs.append((poster, _asset_caption(asset)))
                record["poster_path"] = str(poster.relative_to(root))
                record["export_path"] = record["poster_path"]
                if video_proxies:
                    original = (source_for or {}).get(record["asset_id"])
                    proxy = day_dir / PROXIES_DIRNAME / f"{record['asset_id']}.mp4"
                    if original and _transcode_proxy(original, proxy):
                        record["export_path"] = str(proxy.relative_to(root))
                    else:
                        skipped.append((record["source_filename"], "proxy transcode failed"))
                        record["export_media_type"] = "image/jpeg"
                        record["export_role"] = "poster_frame"
                        record["video_proxy_included"] = False
                continue

            target = media_dir / f"{record['asset_id']}_{record['source_filename']}"
            _link_or_copy(source, target)
            record["export_path"] = str(target.relative_to(root))
            pairs.append((target, _asset_caption(asset)))

        _export_keyframes(day, assets, out_dir, day_dir / KEYFRAMES_DIRNAME, root)

        result = render_contact_sheets(
            pairs,
            cells_per_sheet=CELLS_PER_SHEET,
            columns=SHEET_COLUMNS,
            target_width=SHEET_WIDTH,
        )
        sheets = save_contact_sheets(result, sheet_dir, prefix="contact_sheet")
        skipped.extend((Path(p).name, reason) for p, reason in result.skipped)

        cell_of = {
            str(cell.image_path): cell.index.label
            for sheet in result.sheets
            for cell in sheet.cells
        }
        for record in day["assets"]:
            if record["export_path"]:
                record["cell_id"] = cell_of.get(str(root / record["export_path"]))
        day["sheets"] = [f"{SHEETS_DIRNAME}/{s.name}" for s in sheets]

        brief = day_dir / "brief.md"
        brief.write_text(_render_brief(manifest, day))
        prompt = day_dir / "prompt.md"
        prompt.write_text(_render_prompt(manifest, day))

        packaged.append(
            PackagedDay(
                date=day["date"],
                directory=day_dir,
                sheets=tuple(sheets),
                brief=brief,
                prompt=prompt,
                asset_count=len(day["assets"]),
            )
        )

    manifest_path = root / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    schema_dir = root / SCHEMA_DIRNAME
    schema_dir.mkdir(exist_ok=True)
    shutil.copyfile(SCHEMA_SOURCE, schema_dir / MANIFEST_SCHEMA_FILENAME)
    # The contract for the *answer*, not just the input. A first real run came back
    # richer than requested and non-conformant, and nothing could tell the user.
    shutil.copyfile(STORY_SCHEMA_SOURCE, schema_dir / STORY_SCHEMA_FILENAME)
    (root / "README.md").write_text(_render_readme(manifest, packaged))

    logger.info("package: %d day(s) in %s (%s)", len(packaged), root, mode)
    return Package(
        root=root,
        manifest=manifest_path,
        days=tuple(packaged),
        mode=mode,
        skipped=tuple(skipped),
    )


PROXY_HEIGHT = 720
PROXY_CRF = 28
"""H.264 constant-rate factor. 28 is visibly compressed and small, which is right for a proxy whose
job is letting someone choose a moment, not judge quality."""


def _transcode_proxy(source: Path, target: Path) -> bool:
    """Write a small playable MP4 beside the poster. False if ffmpeg is missing or fails.

    A proxy is what makes an exact source range answerable. Without one, five frames sampled across
    112 seconds cannot support a confident choice of seconds 43-51, and asking for one anyway
    manufactures precision.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"scale=-2:min({PROXY_HEIGHT}\,ih)",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(PROXY_CRF),
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except (OSError, FileNotFoundError):
        return False
    if result.returncode != 0 or not target.exists():
        logger.warning("proxy transcode failed for %s: %s", source.name, result.stderr[-300:])
        target.unlink(missing_ok=True)
        return False
    return True


def _export_keyframes(day: dict, assets: dict, out_dir: Path, target_dir: Path, root: Path) -> None:
    """Copy each clip's extracted frames in, and record where they landed.

    The frames already exist -- the video stage pulled `keyframe_count` of them per clip -- and the
    export simply never carried them. Without them a storyboard has one thumbnail to represent two
    minutes of footage.
    """
    for record in day["assets"]:
        if record["kind"] != "video" or not record.get("video"):
            continue
        source_frames = (assets[record["asset_id"]].get("video") or {}).get("keyframes") or []
        for index, (frame, exported) in enumerate(
            zip(source_frames, record["video"]["keyframes"], strict=False)
        ):
            source = out_dir / frame["path"]
            if not source.exists():
                continue
            target = target_dir / f"{record['asset_id']}_{index:03d}.jpg"
            _link_or_copy(source, target)
            exported["preview_path"] = str(target.relative_to(root))


def _source_path(
    asset: dict, out_dir: Path, mode: str, source_for: dict[str, Path] | None
) -> Path | None:
    if mode == ORIGINALS:
        original = (source_for or {}).get(asset["asset_id"])
        if original is not None:
            return original
    preview = asset.get("preview")
    return out_dir / preview if preview else None


def write_archive(package: Package, target: Path | None = None) -> Path:
    """Zip the package, excluding macOS and Windows filesystem droppings.

    Worth doing here rather than leaving to the user: a Finder-created archive carries `.DS_Store`
    and `__MACOSX` entries, which is exactly what a reviewer noticed about the first one.
    """
    target = target or package.root.with_suffix(".zip")
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.root.rglob("*")):
            if not path.is_file():
                continue
            parts = path.relative_to(package.root).parts
            if any(part in JUNK_NAMES or part.startswith("._") for part in parts):
                continue
            archive.write(path, Path(package.root.name, *parts))
    return target


def _render_readme(manifest: dict, days: list[PackagedDay]) -> str:
    listing = "\n".join(
        f"- `{d.date}/` — {len(d.sheets)} contact sheet(s), {d.asset_count} items" for d in days
    )
    return f"""# {manifest["trip"]["name"] or "Trip"} — ChatGPT package

One directory per day. For each day, open a fresh chat and attach:

1. every `contact_sheet_*.jpg` in that day's folder,
2. `brief.md`,
3. `prompt.md` — then paste its contents as your message.

{listing}

`manifest.json` is the authoritative record: every photo's stable `asset_id`, its content hash,
where it came from, and which contact-sheet cell it landed in. The briefs are generated from it.

**Refer to photos by `asset_id`, not by cell number.** Cell numbers are positional and change
whenever the selection changes; an `asset_id` is a 16-hex-character prefix of the file's content
hash and is stable for as long as the file is. It is not a guess at uniqueness: if two hashes ever
shared a prefix the builder lengthens it for the whole package rather than emitting a duplicate,
and the full hash is recorded beside every id for verification.

`schema/manifest.schema.json` is the JSON Schema for `manifest.json`, so a renderer can validate a
package before trusting it.

**{manifest["package"]["media_note"]}**
"""
