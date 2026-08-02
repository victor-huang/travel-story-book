"""Trip context: the one input the pipeline cannot extract from media.

A photo library records what was photographed. It does not record who was there, whose
voice the journal should speak in, what was planned versus stumbled upon, or what anyone
felt. No amount of better vision models recovers that -- it has to be supplied by hand.

This module is entirely optional in every direction: loading with no file (or `None`)
yields a valid, empty context, and the rest of the pipeline must produce a complete
package without one. When context is supplied, only the fields actually filled in are
used -- names may be omitted or aliased, and a single free-text note is worth more than
any additional structured field.

TOML is the native format -- stdlib, and already the syntax of `config.toml`. **YAML is also
accepted**, because that reversed a decision: the plan sketched YAML, the implementation chose TOML
to avoid a dependency, and then the actual workflow settled it. Asked to summarise a trip, ChatGPT
returns YAML unprompted, and the user's real context file arrived that way. Refusing it would mean
a hand conversion on every trip to save one small, ubiquitous dependency. The format is chosen by
extension.

Validation follows `config.py`'s pattern: unknown keys and bad values raise a `TripContextError`
naming the offending key, rather than being silently ignored. A model-generated file is *more*
likely to carry an unexpected key than a hand-written one, so the loader ignores unknown
**top-level** sections it does not own while still rejecting unknown keys inside the ones it does
-- a context file that also carries the model's own bookkeeping should not be a hard failure.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

VALID_JOURNAL_VOICES = ("first_person_singular", "first_person_plural")


logger = logging.getLogger(__name__)


class TripContextError(Exception):
    """Raised for malformed, unknown, or out-of-range trip context input."""


def _load_yaml(handle: Any, path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a runtime dependency
        raise TripContextError(
            f"{path} is YAML but PyYAML is not installed; convert it to TOML or reinstall"
        ) from exc
    try:
        loaded = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise TripContextError(f"{path} is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise TripContextError(f"{path} must contain a mapping at the top level")
    return loaded


@dataclass(frozen=True, slots=True)
class Traveler:
    """One person on the trip. `name` is optional -- an alias, or nothing at all."""

    role: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class TripContext:
    """Resolved trip context, stored in `trip.json` so both outputs (report + package) see it.

    Every field defaults to empty/None, so `TripContext()` is the valid "no context
    supplied" value. Use `is_empty` to detect that case rather than checking fields
    individually.
    """

    journal_voice: str | None = None
    travelers: tuple[Traveler, ...] = ()
    known_plans: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.journal_voice is not None and self.journal_voice not in VALID_JOURNAL_VOICES:
            options = ", ".join(VALID_JOURNAL_VOICES)
            raise TripContextError(
                f"journal_voice must be one of {options}, got {self.journal_voice!r}"
            )

    @property
    def is_empty(self) -> bool:
        """True when no context was supplied at all, so a consumer can tell it's absent."""
        return not (self.journal_voice or self.travelers or self.known_plans or self.notes)

    @classmethod
    def load(cls, path: Path | None) -> TripContext:
        """Load context from a TOML file. `None` or a missing file yields the empty context."""
        if path is None or not path.exists():
            return cls()
        with path.open("rb") as handle:
            if path.suffix.lower() in {".yaml", ".yml"}:
                raw = _load_yaml(handle, path)
            else:
                raw = tomllib.load(handle)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TripContext:
        raw = dict(raw)

        known = {f.name for f in fields(cls)}
        # Unknown *top-level* sections are dropped with a warning rather than refused. A
        # model-written context carries its own bookkeeping -- `context_id`, `source_policy`,
        # a landmark list -- and none of that should stop a trip from building. Unknown keys
        # *inside* the sections this module owns are still an error, because there the user
        # meant to set something and it silently would not have taken effect.
        ignored = sorted(set(raw) - known)
        if ignored:
            logger.warning(
                "trip context: ignoring %d section(s) this tool does not read: %s",
                len(ignored),
                ", ".join(ignored),
            )
            raw = {k: v for k, v in raw.items() if k in known}

        travelers_raw = raw.pop("travelers", None) or []
        if not isinstance(travelers_raw, list):
            raise TripContextError(
                f"trip_context.travelers must be a list, got {type(travelers_raw).__name__}"
            )
        travelers = tuple(_build_traveler(item, index) for index, item in enumerate(travelers_raw))

        known_plans = tuple(raw.pop("known_plans", None) or [])
        notes = tuple(raw.pop("notes", None) or [])

        try:
            return cls(
                journal_voice=raw.pop("journal_voice", None),
                travelers=travelers,
                known_plans=known_plans,
                notes=notes,
            )
        except TypeError as exc:
            raise TripContextError(f"invalid trip context: {exc}") from exc

    def render(self) -> str:
        """Render the prose block T41 (the ChatGPT package) pastes into `brief.md`.

        Describes only what was actually supplied -- it never invents a traveler, plan,
        or feeling. If nothing was supplied at all, delegate to `render_absent()` so the
        model is told explicitly to stay factual rather than receiving a blank section.
        """
        if self.is_empty:
            return self.render_absent()

        lines = ["## Trip context", ""]
        lines.append(
            "The following was supplied by the traveler, not inferred from the media. "
            "Use it to inform tone and narration; do not invent anything beyond it."
        )
        lines.append("")

        if self.journal_voice:
            voice_desc = {
                "first_person_singular": 'first person singular ("I")',
                "first_person_plural": 'first person plural ("we")',
            }[self.journal_voice]
            lines.append(f"- Journal voice: {voice_desc}")

        if self.travelers:
            traveler_bits = []
            for traveler in self.travelers:
                traveler_bits.append(
                    f"{traveler.role} ({traveler.name})" if traveler.name else traveler.role
                )
            lines.append(f"- Travelers: {', '.join(traveler_bits)}")

        if self.known_plans:
            lines.append("- Known plans:")
            for plan in self.known_plans:
                lines.append(f"  - {plan}")

        if self.notes:
            lines.append("- Notes from the traveler:")
            for note in self.notes:
                lines.append(f"  - {note}")

        return "\n".join(lines) + "\n"

    @staticmethod
    def render_absent() -> str:
        """Render the "no context supplied" block: explicit, so the model does not invent one.

        An invented emotion is the same class of failure as an invented quote -- this
        tells the model that no traveler input exists rather than leaving the absence
        implicit, where it might be filled in from imagination.
        """
        return (
            "## Trip context\n\n"
            "No trip context was supplied by the traveler: no traveler names or roles, "
            "no journal voice preference, no known plans, and no notes. Stay strictly "
            "factual -- describe only what is visible in the media and known from its "
            "metadata. Do not invent who was present, how anyone felt, or what was "
            "planned.\n"
        )


def _build_traveler(raw: Any, index: int) -> Traveler:
    if not isinstance(raw, dict):
        raise TripContextError(f"trip_context.travelers[{index}] must be a table")

    raw = dict(raw)
    # `display_name` is what a model calls it, and the meaning is unambiguous.
    if "display_name" in raw and "name" not in raw:
        raw["name"] = raw.pop("display_name")

    known = {f.name for f in fields(Traveler)}
    # Dropped with a warning, not refused. This is where a model-written file puts its own
    # bookkeeping -- `id`, `age_during_trip`, `count` -- and losing a build over an extra key in a
    # list of names would be absurd. The strictness that matters is on the scalar settings above,
    # where a typo means a deliberate choice silently did nothing.
    ignored = sorted(set(raw) - known)
    if ignored:
        logger.warning(
            "trip context: ignoring traveler field(s) this tool does not read: %s",
            ", ".join(ignored),
        )
        raw = {k: v for k, v in raw.items() if k in known}

    if "role" not in raw:
        raise TripContextError(f"trip_context.travelers[{index}] is missing required key: role")

    try:
        return Traveler(role=raw["role"], name=raw.get("name"))
    except TypeError as exc:
        raise TripContextError(f"invalid trip_context.travelers[{index}]: {exc}") from exc
