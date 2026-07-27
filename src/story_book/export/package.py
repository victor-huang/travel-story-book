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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from story_book.export.contact_sheet import render_contact_sheets, save_contact_sheets
from story_book.export.report import clock, country_name, duration, place_label

logger = logging.getLogger(__name__)

PACKAGE_DIRNAME = "package"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1

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
    return {
        "duration_seconds": _round(video["duration_seconds"], 1),
        "motion_score": _round(video["motion_score"]),
        "keyframe_count": len(video["keyframes"]),
        # The distinction P02 asked for. `no_speech` is a measured negative; `not_processed`
        # means nobody listened, and a storyboard must not treat the two the same way.
        "transcript_status": video["transcript_status"],
        "transcript_text": (transcript or {}).get("text"),
    }


def _asset_record(asset: dict, day: str, event_id: str | None, export_path: str | None) -> dict:
    location = asset.get("location")
    place = (location or {}).get("place")
    return {
        "asset_id": asset["asset_id"],
        "content_hash": asset["content_hash"],
        "source_filename": asset["filename"],
        "kind": asset["kind"],
        "day": day,
        "event_id": event_id,
        "taken_local": asset["taken_local"],
        "export_path": export_path,
        "cell_id": None,
        "place": (
            {
                "name": place_label(place),
                "city": place.get("city"),
                "country": country_name(place.get("country")),
            }
            if place
            else None
        ),
        "quality": _quality_summary(asset),
        "video": _video_summary(asset),
        "pinned_by_human": asset.get("selected", {}).get("day", {}).get("reason") == "pinned",
    }


def _event_record(event: dict, asset_ids: list[str]) -> dict:
    location = event["location"]
    return {
        "event_id": event["id"],
        "label": event["label"],
        "place": place_label(event["place"]) or None,
        "start_local": event["start_local"],
        "end_local": event["end_local"],
        "duration": duration(event["duration_minutes"]),
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


def build_manifest(doc: dict, mode: str) -> dict[str, Any]:
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
        days.append(
            {
                "date": day["date"],
                "counts": day["counts"],
                "gps_coverage": day["gps_coverage"],
                "events": [
                    _event_record(
                        event, [a for a in event["assets"] if a in {m["asset_id"] for m in members}]
                    )
                    for event in day["events"]
                ],
                "assets": [
                    _asset_record(a, day["date"], event_of.get(a["asset_id"]), None)
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
        },
        "trip": {
            "name": doc["trip"]["name"],
            "start_local": doc["trip"]["start_local"],
            "end_local": doc["trip"]["end_local"],
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
        f"{day['counts']['media']} items captured ({day['counts']['images']} photos, "
        f"{day['counts']['videos']} videos) across {day['counts']['events']} stops. "
        f"{len(day['assets'])} are included here.",
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

    for position, event in enumerate(day["events"], start=1):
        if not event["asset_ids"]:
            continue
        location = event["location"]
        header = event["label"] or event["place"] or "Unnamed stop"
        lines.append(f"### Stop {position} · {clock(event['start_local'])} · {header}")
        facts = [
            f"{clock(event['start_local'])}–{clock(event['end_local'])}",
            event["duration"],
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
        for asset_id in event["asset_ids"]:
            asset = by_id.get(asset_id)
            if asset is None:
                continue
            lines.append(_brief_line(asset, event_place=event["place"]))
        lines.append("")

    videos = [a for a in day["assets"] if a["kind"] == "video"]
    if videos:
        lines += ["## Video", "", "Available footage, for the storyboard:", ""]
        for video in videos:
            info = video["video"]
            status = {
                "transcribed": "speech transcribed",
                "no_speech": "processed, no speech found",
                "not_processed": "not analysed for speech",
            }[info["transcript_status"]]
            lines.append(
                f"- `{video['asset_id']}` {clock(video['taken_local'])} · "
                f"{_seconds(video)} · {status}"
            )
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
        if asset["pinned_by_human"]:
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


def _brief_line(asset: dict, event_place: str | None = None) -> str:
    bits = [f"- `{asset['asset_id']}`", clock(asset["taken_local"])]
    if asset["cell_id"]:
        bits.append(f"(sheet {asset['cell_id']})")
    if asset["kind"] == "video":
        bits.append(f"**video, {_seconds(asset)}**")
    # Only when it differs from the stop's own place. Repeating "Vienna, Austria" on thirty-three
    # consecutive lines is noise that buries the lines which do carry information.
    if asset["place"] and asset["place"]["name"] != event_place:
        bits.append(f"— {asset['place']['name']}")
    if asset["pinned_by_human"]:
        bits.append("**[chosen by the traveller]**")
    if asset["quality"]:
        quality = asset["quality"]
        parts = [f"q={quality['overall']}"]
        if quality["faces_detected"]:
            parts.append(f"{quality['faces_detected']} face(s)")
        bits.append(f"({', '.join(parts)})")
    return " ".join(bits)


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
4. **A video storyboard** using the clips listed under "Video" in the brief. Note that
   `no speech found` is a *measured* result, not an unexamined clip.

## Then repeat it as JSON

After the prose, output a single fenced ```json block with this shape. This is what a renderer
consumes, so keep the keys exactly as written:

```json
{{
  "day": "{day["date"]}",
  "chapters": [
    {{"title": "", "starts_at": "HH:MM", "summary": "", "asset_ids": []}}
  ],
  "captions": [{{"asset_id": "", "caption": ""}}],
  "layout_pages": [
    {{"page": 1, "hero_asset_id": "", "asset_ids": [], "note": ""}}
  ],
  "video_scenes": [
    {{"asset_id": "", "role": "", "suggested_seconds": 0, "note": ""}}
  ],
  "uncertainties": [""],
  "requested_additional_context": [""]
}}
```

`uncertainties` is not optional politeness — list anything you inferred rather than read, and
anything the sheets were too small to judge. `requested_additional_context` is what you would
need to write a better entry.

**Do not invent place names, landmark names, or times.** The brief carries what the pipeline
actually resolved; if something is unnamed there, it is unnamed. Say so rather than guessing.
"""


def build_package(
    doc: dict,
    out_dir: Path,
    *,
    mode: str = PREVIEW,
    source_for: dict[str, Path] | None = None,
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

    manifest = build_manifest(doc, mode)
    assets = doc["assets"]
    packaged: list[PackagedDay] = []
    skipped: list[tuple[str, str]] = []

    for day in manifest["days"]:
        if not day["assets"]:
            continue
        day_dir = root / day["date"]
        media_dir = day_dir / ("full" if mode == ORIGINALS else "media")
        day_dir.mkdir(parents=True)

        pairs: list[tuple[Path, str]] = []
        for record in day["assets"]:
            asset = assets[record["asset_id"]]
            source = _source_path(asset, out_dir, mode, source_for)
            if source is None or not source.exists():
                skipped.append((record["source_filename"], "no exportable file"))
                continue
            target = media_dir / f"{record['asset_id']}_{record['source_filename']}"
            _link_or_copy(source, target)
            record["export_path"] = str(target.relative_to(root))
            pairs.append((target, _asset_caption(asset)))

        result = render_contact_sheets(
            pairs,
            cells_per_sheet=CELLS_PER_SHEET,
            columns=SHEET_COLUMNS,
            target_width=SHEET_WIDTH,
        )
        sheets = save_contact_sheets(result, day_dir, prefix="contact_sheet")
        skipped.extend((Path(p).name, reason) for p, reason in result.skipped)

        cell_of = {
            str(cell.image_path): cell.index.label
            for sheet in result.sheets
            for cell in sheet.cells
        }
        for record in day["assets"]:
            if record["export_path"]:
                record["cell_id"] = cell_of.get(str(root / record["export_path"]))
        day["sheets"] = [s.name for s in sheets]

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
    (root / "README.md").write_text(_render_readme(manifest, packaged))

    logger.info("package: %d day(s) in %s (%s)", len(packaged), root, mode)
    return Package(
        root=root,
        manifest=manifest_path,
        days=tuple(packaged),
        mode=mode,
        skipped=tuple(skipped),
    )


def _source_path(
    asset: dict, out_dir: Path, mode: str, source_for: dict[str, Path] | None
) -> Path | None:
    if mode == ORIGINALS:
        original = (source_for or {}).get(asset["asset_id"])
        if original is not None:
            return original
    preview = asset.get("preview")
    return out_dir / preview if preview else None


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
whenever the selection changes.

**{manifest["package"]["media_note"]}**
"""
