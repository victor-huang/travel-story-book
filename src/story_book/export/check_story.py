"""Check a model's `story.json` against the package it was written from.

The prompt asks for a JSON shape and nothing enforced it. The first real run came back richer than
requested and *not conformant*: thirteen chapters with no `source_event_ids`, `video_scenes`
renamed to `video_storyboard`, `uncertainties` renamed, `layout_pages` absent. A renderer built to
the contract would have failed on it, and the only way to find out was to read the file by hand.

Two things are checked, and the second is the one that matters:

* **Shape** -- against `story_schema.json`, the published contract.
* **Grounding** -- every `asset_id` and `event_id` must resolve against the manifest. A caption
  attached to an id that does not exist is worse than a missing caption: it looks like a fact.

The same run also showed why grounding is worth checking separately from shape. Every one of its 56
asset references resolved, and the accompanying *prose* still moved the Plague Column two days,
because prose has nothing anchoring it. Ids keep a document honest; sentences do not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STORY_SCHEMA = Path(__file__).parent / "story_schema.json"


@dataclass(slots=True)
class StoryReport:
    schema_errors: list[str] = field(default_factory=list)
    unknown_assets: list[tuple[str, str]] = field(default_factory=list)
    """`(asset_id, where)` for references the package does not contain."""
    unknown_events: list[tuple[str, str]] = field(default_factory=list)
    cross_day_assets: list[tuple[str, str, str]] = field(default_factory=list)
    """`(chapter, asset_id, "chapter=<date> asset=<date>")` -- a chapter citing another day."""
    uncited_assets: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    cited: int = 0
    available: int = 0

    @property
    def ok(self) -> bool:
        """Shape and grounding. An uncited asset is information, not an error."""
        return not (
            self.schema_errors
            or self.unknown_assets
            or self.unknown_events
            or self.cross_day_assets
        )

    @property
    def coverage(self) -> float:
        return self.cited / self.available if self.available else 0.0


def _collect_ids(node: Any, path: str = "story") -> list[tuple[str, str]]:
    """Every asset reference in the document, wherever the model chose to put it.

    Walks the whole tree rather than the known keys, because the point of the check is that the
    model may not have used the keys it was asked for -- and a reference in an unexpected place
    still has to resolve.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            if key in {"asset_id", "hero_asset_id"} and isinstance(value, str):
                found.append((value, here))
            elif key.endswith("asset_ids") and isinstance(value, list):
                found += [(v, here) for v in value if isinstance(v, str)]
            else:
                found += _collect_ids(value, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += _collect_ids(item, f"{path}[{index}]")
    return found


def check_story(story: dict, manifest: dict) -> StoryReport:
    report = StoryReport()

    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is a dev dependency
        report.schema_errors.append("jsonschema is not installed; shape was not checked")
    else:
        schema = json.loads(STORY_SCHEMA.read_text())
        validator = jsonschema.Draft202012Validator(schema)
        report.schema_errors = [
            f"{'.'.join(str(p) for p in e.absolute_path) or 'story'}: {e.message}"
            for e in sorted(validator.iter_errors(story), key=lambda e: list(e.absolute_path))
        ]

    assets = {a["asset_id"]: a for day in manifest["days"] for a in day["assets"]}
    events = {e["event_id"] for day in manifest["days"] for e in day["events"]}
    report.available = len(assets)

    referenced = _collect_ids(story)
    seen = set()
    for asset_id, where in referenced:
        if asset_id not in assets:
            report.unknown_assets.append((asset_id, where))
        else:
            seen.add(asset_id)
    report.cited = len(seen)
    report.uncited_assets = sorted(
        f"{assets[a]['source_filename']} ({a})" for a in assets if a not in seen
    )

    for chapter in story.get("chapters", []) or []:
        if not isinstance(chapter, dict):
            continue
        name = str(chapter.get("chapter_id") or chapter.get("title") or "chapter")
        for event_id in chapter.get("source_event_ids", []) or []:
            if event_id not in events:
                report.unknown_events.append((event_id, name))
        date = chapter.get("date")
        for asset_id in chapter.get("asset_ids", []) or []:
            asset = assets.get(asset_id)
            if asset and date and asset["day"] != date:
                report.cross_day_assets.append(
                    (name, asset_id, f"chapter={date} asset={asset['day']}")
                )

    for optional in ("layout_pages", "video_scenes", "requested_additional_context"):
        if not story.get(optional):
            report.missing_optional.append(optional)

    return report
