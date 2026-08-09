"""Scaffold a trip directory: a config derived from a profile, with corrections beside it.

The friction this removes is a transcription step. `profile` measures numbers from real media
and then prints "copy these into config.toml", which is where a measurement goes to be retyped
wrongly. `init` writes them itself.

It writes by *rewriting the shipped example* rather than emitting a fresh file, so every comment
explaining what a threshold means survives, and a key added to `config.example.toml` later
appears in new trips without anyone touching this module. Only keys the profile actually
measured are given a value; everything else stays at its documented default. A plausible number
the profiler did not measure is the failure this project keeps repeating, so it is not written.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from story_book.profile import Profile, suggestions

CONFIG_NAME = "config.toml"
OVERRIDES_NAME = "overrides.toml"
EXAMPLE_CONFIG = Path(__file__).parent / "config.example.toml"
EXAMPLE_OVERRIDES = Path(__file__).parent / "overrides.example.toml"

_TABLE = re.compile(r"^#?\s*\[([^\]]+)\]\s*$")
_ASSIGNMENT = re.compile(r"^(?:#\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class InitError(Exception):
    """A scaffold could not be written, with a reason a person can act on."""


@dataclass(frozen=True, slots=True)
class Setting:
    """One value written into the config, and the justification written beside it.

    `origin` is not decoration. A threshold derived from this folder's own media and a
    coordinate copied from last year's trip are different kinds of claim, and labelling the
    second "measured" would be this project's most-repeated failure in miniature.
    """

    value: str  # a TOML literal: a string arrives already quoted
    basis: str | None = None
    origin: str = "measured"


@dataclass(frozen=True, slots=True)
class InitPlan:
    """What `init` wrote, so the caller can report it without re-reading the files."""

    trip_dir: Path
    config_path: Path
    overrides_path: Path
    settings: dict[str, Setting]
    home_source: Path | None
    overrides_existed: bool


def render_config(example: str, settings: dict[str, Setting], *, header: str | None = None) -> str:
    """Rewrite the example config, replacing the named dotted keys with measured values.

    A key that does not appear in the example is an error, not a silent omission: it means the
    profiler suggests something the config file cannot express, and writing the file anyway
    would hide that.
    """
    pending = dict(settings)
    table = ""
    out: list[str] = []
    lines = example.splitlines()

    if header is not None:
        # The example opens with "Copy to config.toml and edit", which is instructions for a
        # file this no longer is.
        while lines and lines[0].startswith("#"):
            lines.pop(0)
        out.extend(header.splitlines())

    for line in lines:
        table_header = _TABLE.match(line)
        if table_header:
            table = table_header.group(1)
            if line.lstrip().startswith("#") and any(
                key.startswith(f"{table}.") for key in pending
            ):
                line = f"[{table}]"
            out.append(line)
            continue

        assignment = _ASSIGNMENT.match(line.strip())
        if assignment:
            name = assignment.group(1)
            key = f"{table}.{name}" if table else name
            setting = pending.pop(key, None)
            if setting is not None:
                if setting.basis:
                    out.append(f"# {setting.origin}: {setting.basis}")
                out.append(f"{name} = {setting.value}")
                continue

        out.append(line)

    if pending:
        missing = ", ".join(sorted(pending))
        raise InitError(
            f"{EXAMPLE_CONFIG.name} has no key(s) named: {missing}. "
            "The example and the config schema have drifted apart."
        )
    return "\n".join(out) + "\n"


def home_settings(config_path: Path) -> dict[str, Setting]:
    """The `[home]` block of an existing config, so a second trip reuses one address.

    Retyping coordinates is how a privacy filter ends up pointed at the wrong house, and the
    exclusion is the guarantee with the least margin for a typo.
    """
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError as exc:
        raise InitError(f"could not read {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise InitError(f"{config_path} is not valid TOML: {exc}") from exc

    home = raw.get("home")
    if not isinstance(home, dict) or not home:
        raise InitError(f"{config_path} has no [home] block to copy")

    basis = f"from {config_path}"
    return {
        f"home.{name}": Setting(
            _toml_literal(value), basis if name == "lat" else None, origin="copied"
        )
        for name, value in home.items()
    }


def face_detector_setting(model: Path, *, basis: str) -> dict[str, Setting]:
    """An absolute path to the YuNet model, verified to exist.

    Relative here is a trap. The path resolves against the working directory, and a miss only
    warns -- the face signal then drops out of the quality score, which is the difference
    between a book of family photographs and a book of parked vans. Checking once at scaffold
    time is cheaper than noticing it in the highlights.
    """
    resolved = model.expanduser().resolve()
    if not resolved.is_file():
        raise InitError(
            f"face detector model not found at {resolved}. Fetch it once with:\n"
            "  curl -sLo models/face_detection_yunet_2023mar.onnx https://github.com/opencv/"
            "opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx\n"
            "or pass --no-face-model to build without the face signal."
        )
    return {
        "models.face_detector_model": Setting(
            _toml_literal(str(resolved)), basis, origin="verified"
        )
    }


def resolve_face_model(raw: str, *, relative_to: Path) -> Path:
    """Locate a possibly-relative model path from another config, trying both plausible roots."""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    for root in (Path.cwd(), relative_to):
        if (root / candidate).is_file():
            return root / candidate
    raise InitError(
        f"{raw!r} is relative and was not found under {Path.cwd()} or {relative_to}. "
        "Pass --face-model with an absolute path."
    )


def plan_settings(
    profile: Profile,
    *,
    trip_name: str | None = None,
    home: dict[str, Setting] | None = None,
    face_model: dict[str, Setting] | None = None,
) -> dict[str, Setting]:
    """Every value `init` will write, measured first and inherited second."""
    settings: dict[str, Setting] = {}
    if trip_name:
        settings["trip_name"] = Setting(_toml_literal(trip_name), None, origin="given")
    settings.update(home or {})
    settings.update(face_model or {})
    for key, value, basis in suggestions(profile):
        settings[key] = Setting(value, basis)
    return settings


def starter_overrides(example: str) -> str:
    """The example corrections file with every entry commented out.

    Copying the example verbatim does not scaffold a trip, it breaks one: its entries name real
    files from the trip it was written for, and a `pin` for a photo this library does not
    contain fails the run. Commenting them out is derived from the example rather than kept as a
    second file, so a section added there cannot go missing here.
    """
    out = [
        "# Corrections for this trip. Every example below is commented out -- uncomment and",
        "# edit, addressing media by filename or by the asset id shown in the report.",
        "",
    ]
    for line in example.splitlines():
        stripped = line.strip()
        keep = not stripped or stripped.startswith("#") or stripped.startswith("override_version")
        out.append(line if keep else f"# {line}")
    return "\n".join(out) + "\n"


def config_header(source: Path, settings: dict[str, Setting]) -> str:
    """The banner on a generated config: where it came from and what to trust in it."""
    counted = sum(1 for s in settings.values() if s.origin == "measured")
    return (
        f"# Travel Story Book configuration for {source}.\n"
        f"# Written by `story-book init` from a profile of that folder.\n"
        f"# {counted} value(s) below are marked with the observation behind them; every other\n"
        f"# value is the shipped default, not a measurement.\n"
        f"# Edit freely -- nothing rewrites this file.\n"
    )


def write_trip_dir(
    trip_dir: Path,
    settings: dict[str, Setting],
    *,
    source: Path,
    home_source: Path | None = None,
) -> InitPlan:
    """Create the directory and write both files, refusing to overwrite an existing config."""
    trip_dir = trip_dir.expanduser()
    config_path = trip_dir / CONFIG_NAME
    overrides_path = trip_dir / OVERRIDES_NAME

    if config_path.exists():
        raise InitError(f"{config_path} already exists; init will not overwrite it")

    trip_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        render_config(EXAMPLE_CONFIG.read_text(), settings, header=config_header(source, settings))
    )

    overrides_existed = overrides_path.exists()
    if not overrides_existed:
        overrides_path.write_text(starter_overrides(EXAMPLE_OVERRIDES.read_text()))

    return InitPlan(
        trip_dir=trip_dir,
        config_path=config_path,
        overrides_path=overrides_path,
        settings=settings,
        home_source=home_source,
        overrides_existed=overrides_existed,
    )


def next_steps(source: Path, plan: InitPlan) -> list[str]:
    """The commands to run next, with the paths already filled in.

    `build` is deliberately not run for you: it is an overnight job whose log carries the
    timezone conflicts and the missing-model warnings, and a scaffold that swallows it into a
    progress bar is how those go unread.
    """
    out = plan.trip_dir / "out"
    config = plan.config_path
    return [
        f"story-book build {source} --out {out} --config {config}",
        f"open {out / 'report' / 'index.html'}   # then correct picks in {plan.overrides_path}",
        f"story-book package --out {out} --config {config} --zip",
        f"story-book report --out {out} --config {config}   # after story.json lands in out/story/",
        f"story-book reel --out {out} --config {config} --source {source} --music <file>",
    ]


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'
