"""Module 12: human corrections, applied at build time.

The report is read-only and regenerated, so every correction a human wants to make lives in a
hand-edited `overrides.toml` next to the config. `build` reads it, and because the expensive
stages are cached by content hash, re-running after an edit costs seconds.

**Everything is addressed by filename or `asset_id`, never by cluster or event id.** Cluster and
event ids are assigned fresh on every run -- both are rebuilt from scratch each time -- so an
override saying `cluster = 12` would quietly come to mean a different group of photos the next time
the library changed. A filename is stable for as long as the file is, and an `asset_id` is a prefix
of the content hash, so both are stable for the same reason: they are functions of the file, not of
insertion order. Events are therefore addressed by naming a photo inside them.

Why this exists at all: selection ranks on *technical* quality, and a human ranks on what the
photo is of. Measured against 19 hand-labelled decisions on a real trip, no setting of the
quota, time-spacing or diversity thresholds recovered more than 7 of 16 requested photos, and
that only by doubling the size of the book. The gap is not a tuning problem, so it gets a
mechanism instead of a threshold.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OVERRIDES_SCHEMA_VERSION = 1

logger = logging.getLogger(__name__)


class OverrideError(Exception):
    """Raised for a malformed overrides file, or one naming media that is not in the library.

    Unresolvable names are fatal rather than skipped. A silently ignored override looks exactly
    like a correction that did not work, and the whole point of the file is that the human gets
    the last word.
    """


@dataclass(frozen=True, slots=True)
class EventLabel:
    photo: str
    label: str


@dataclass(frozen=True, slots=True)
class EventSplit:
    before: str


@dataclass(frozen=True, slots=True)
class EventMerge:
    photos: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LandmarkLabel:
    name: str
    label: str


@dataclass(frozen=True, slots=True)
class Overrides:
    """The file as written, before any of it is matched against the library."""

    pin: tuple[str, ...] = ()
    reject: tuple[str, ...] = ()
    keeper: tuple[str, ...] = ()
    label_event: tuple[EventLabel, ...] = ()
    split_event: tuple[EventSplit, ...] = ()
    merge_events: tuple[EventMerge, ...] = ()
    label_landmark: tuple[LandmarkLabel, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.pin,
                self.reject,
                self.keeper,
                self.label_event,
                self.split_event,
                self.merge_events,
                self.label_landmark,
            )
        )

    @classmethod
    def load(cls, path: Path | None) -> Overrides:
        if path is None or not path.exists():
            return cls()
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        return cls.from_dict(raw, source=str(path))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source: str = "overrides") -> Overrides:
        raw = dict(raw)
        version = raw.pop("override_version", OVERRIDES_SCHEMA_VERSION)
        if version != OVERRIDES_SCHEMA_VERSION:
            raise OverrideError(
                f"override_version {version} is not supported (expected {OVERRIDES_SCHEMA_VERSION})"
            )

        known = {
            "pin",
            "reject",
            "keeper",
            "label_event",
            "split_event",
            "merge_events",
            "label_landmark",
        }
        unknown = set(raw) - known
        if unknown:
            raise OverrideError(
                f"unknown key(s) in {source}: {', '.join(sorted(unknown))}. "
                f"Valid keys: {', '.join(sorted(known))}"
            )

        overrides = cls(
            pin=_names(raw.get("pin"), f"{source}.pin"),
            reject=_names(raw.get("reject"), f"{source}.reject"),
            keeper=_names(raw.get("keeper"), f"{source}.keeper"),
            label_event=tuple(
                _table(EventLabel, item, f"{source}.label_event")
                for item in raw.get("label_event") or ()
            ),
            split_event=tuple(
                _table(EventSplit, item, f"{source}.split_event")
                for item in raw.get("split_event") or ()
            ),
            merge_events=tuple(
                EventMerge(_names(item.get("photos"), f"{source}.merge_events.photos"))
                for item in raw.get("merge_events") or ()
            ),
            label_landmark=tuple(
                _table(LandmarkLabel, item, f"{source}.label_landmark")
                for item in raw.get("label_landmark") or ()
            ),
        )

        conflict = set(overrides.pin) & set(overrides.reject)
        if conflict:
            raise OverrideError(
                f"{', '.join(sorted(conflict))} is both pinned and rejected in {source}"
            )
        for merge in overrides.merge_events:
            if len(merge.photos) < 2:
                raise OverrideError(
                    f"{source}.merge_events needs at least two photos, naming the events to join"
                )
        return overrides


def _names(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OverrideError(f"{where} must be a list of filenames or asset ids")
    return tuple(value)


def _table[T](cls: type[T], raw: Any, where: str) -> T:
    if not isinstance(raw, dict):
        raise OverrideError(f"{where} must be a table, got {type(raw).__name__}")
    try:
        return cls(**raw)
    except TypeError as exc:
        raise OverrideError(f"invalid {where}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ResolvedOverrides:
    """`Overrides` with every filename matched to a content hash in this library."""

    pin: frozenset[str] = frozenset()
    reject: frozenset[str] = frozenset()
    keeper: frozenset[str] = frozenset()
    event_labels: dict[str, str] = field(default_factory=dict)
    split_before: frozenset[str] = frozenset()
    merge_groups: tuple[tuple[str, ...], ...] = ()
    landmark_labels: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.pin,
                self.reject,
                self.keeper,
                self.event_labels,
                self.split_before,
                self.merge_groups,
                self.landmark_labels,
            )
        )


ASSET_ID_PATTERN = re.compile(r"^[0-9a-f]{8,}$")
"""An `asset_id` from `trip.json` or the HTML report: a lowercase hex prefix of the content hash.

Accepted alongside filenames because it is **stable for the same reason a filename is** -- it is a
function of the file's bytes, not of insertion order. Cluster and event ids are still refused; those
are reassigned on every run, so an override naming one would come to mean a different group of
photographs as soon as the library changed.
"""


def resolve(overrides: Overrides, conn: sqlite3.Connection) -> ResolvedOverrides:
    """Match every filename or asset id in the overrides to exactly one media hash.

    A reference that matches nothing, or more than one file, raises rather than being dropped.
    """
    by_name: dict[str, list[str]] = {}
    hashes: list[str] = []
    for row in conn.execute("SELECT hash, path FROM media"):
        name = Path(row["path"]).name
        by_name.setdefault(name, []).append(row["hash"])
        by_name.setdefault(Path(name).stem, []).append(row["hash"])
        hashes.append(row["hash"])

    def one(name: str, where: str) -> str:
        matches = by_name.get(name) or by_name.get(Path(name).stem)
        if not matches and ASSET_ID_PATTERN.match(name.lower()):
            # The report prints the asset id next to the filename, so it is the thing under a
            # reader's cursor while they are deciding what to pin.
            prefix = name.lower()
            matches = [h for h in hashes if h.startswith(prefix)]
            if len(set(matches)) > 1:
                raise OverrideError(
                    f"{where}: asset id {name!r} matches {len(set(matches))} files -- "
                    "use more characters of the id"
                )
        if not matches:
            raise OverrideError(
                f"{where}: no media in this library is named {name!r}, and it is not an asset id "
                "from the report or trip.json"
            )
        if len(set(matches)) > 1:
            raise OverrideError(
                f"{where}: {name!r} matches {len(set(matches))} different files; "
                "use a path suffix that is unique"
            )
        return matches[0]

    return ResolvedOverrides(
        pin=frozenset(one(n, "pin") for n in overrides.pin),
        reject=frozenset(one(n, "reject") for n in overrides.reject),
        keeper=frozenset(one(n, "keeper") for n in overrides.keeper),
        event_labels={one(item.photo, "label_event"): item.label for item in overrides.label_event},
        split_before=frozenset(one(item.before, "split_event") for item in overrides.split_event),
        merge_groups=tuple(
            tuple(one(name, "merge_events") for name in merge.photos)
            for merge in overrides.merge_events
        ),
        landmark_labels={item.name: item.label for item in overrides.label_landmark},
    )
