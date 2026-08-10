"""The trip's working directory: materialise the media, then scaffold a config over it.

Ingest hands the pipeline a folder, and the pipeline's entire input contract is that folder. Two
things happen here and the order matters.

**Materialise.** The bytes are in the object store, never on the API server's disk on their way
through, so a build needs them fetched. Every file is written under the name
`assign_stored_names` chose, because `overrides.toml` addresses media by filename and a
disambiguated collision has to be the same string every time.

**Scaffold, with `story-book init --trip-dir`, and only over a complete folder.** `init` *profiles*
the source and writes what it measured, which is the whole reason not to hand-write a config -- a
retyped threshold is a measurement that became a guess. But it also means the folder has to be
there first: `init` over an empty directory succeeds, warns "no importable media found", and writes
a config in which nothing was measured. Scaffolding at trip-creation time would produce exactly
that file, silently, and every later build would inherit defaults dressed as measurements. So
scaffolding waits for the media, and refuses while any declared asset is still missing.

And the scaffolded `overrides.toml` is **loaded in this new context and asserted empty** before
this module reports success. `story-book init` once scaffolded by copying
`overrides.example.toml`, whose worked example pins photographs from another trip, and the first
build it told you to run died. That is fixed upstream; this checks it here anyway, because the
check is one line and the failure it catches is a build that dies on someone else's filenames.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from story_book.overrides import Overrides
from storybook_service.index import TripAsset
from storybook_service.naming import asset_key
from storybook_service.objectstore import ObjectStore
from storybook_service.settings import Settings

CONFIG_NAME = "config.toml"
OVERRIDES_NAME = "overrides.toml"
SOURCE_DIR = "source"
OUT_DIR = "out"


class ScaffoldError(Exception):
    """The trip directory could not be prepared, with a reason a person can act on."""


@dataclass(frozen=True, slots=True)
class TripPaths:
    """Where one trip's files live. `out` is the CLI's `--out`, `source` is its positional arg."""

    trip_dir: Path

    @property
    def source(self) -> Path:
        return self.trip_dir / SOURCE_DIR

    @property
    def out(self) -> Path:
        return self.trip_dir / OUT_DIR

    @property
    def config(self) -> Path:
        return self.trip_dir / CONFIG_NAME

    @property
    def overrides(self) -> Path:
        return self.trip_dir / OVERRIDES_NAME


def trip_paths(settings: Settings, trip_id: str) -> TripPaths:
    return TripPaths(settings.data_root / "trips" / trip_id)


@dataclass(slots=True)
class MaterialiseResult:
    fetched: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    """Declared at negotiate time and not in the store: an upload that never happened.

    Reported rather than skipped over. A folder that is quietly short of four photographs builds
    perfectly and produces a trip missing an afternoon.
    """
    removed: list[str] = field(default_factory=list)
    """Files in the folder that the trip no longer claims under that name.

    They arise when a later negotiate renames an asset -- a second camera's `IMG_0001.JPG` renames
    *both* copies, because the stored name is a function of the whole set rather than of arrival
    order. Left in place, the old name would be scanned as an extra photograph and the trip would
    contain the same picture twice.
    """

    @property
    def complete(self) -> bool:
        return not self.missing


def materialise_source(
    *,
    settings: Settings,
    store: ObjectStore,
    owner_id: str,
    trip_id: str,
    assets: list[TripAsset],
) -> MaterialiseResult:
    paths = trip_paths(settings, trip_id)
    paths.source.mkdir(parents=True, exist_ok=True)
    result = MaterialiseResult()

    for asset in assets:
        destination = paths.source / asset.stored_filename
        # Size, not existence. An interrupted materialisation leaves a partial file, and treating
        # presence as completion would feed a truncated photograph to the scanner -- the same
        # mistake I15's ledger exists to avoid on the phone.
        if destination.exists() and destination.stat().st_size == asset.size:
            result.already_present.append(asset.media_hash)
            continue
        key = asset_key(
            scope=settings.asset_scope,
            prefix=settings.s3_asset_prefix,
            owner_id=owner_id,
            media_hash=asset.media_hash,
        )
        info = store.head(key)
        if info is None or info.size != asset.size:
            result.missing.append(asset.media_hash)
            continue
        store.get_to_file(key, destination)
        result.fetched.append(asset.media_hash)

    # Reconcile: this folder is derived from the object store, so it is the service's to rebuild --
    # it is not the traveller's source tree, and guarantee 1 is about that tree. Anything here the
    # trip does not claim under that name is a leftover from a rename, and the scanner would read it
    # as another photograph.
    expected = {asset.stored_filename for asset in assets}
    for stale in sorted(paths.source.iterdir()):
        if stale.is_file() and stale.name not in expected:
            stale.unlink()
            result.removed.append(stale.name)
    return result


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    config: Path
    overrides: Path
    created: bool
    """False when a config was already there.

    `story-book init` refuses to overwrite one, which is the behaviour this wants: a config a user
    has edited must survive the next upload.
    """


def scaffold_trip(
    *,
    settings: Settings,
    trip_id: str,
    trip_name: str,
    like: Path | None = None,
) -> ScaffoldResult:
    """Run `story-book init --trip-dir`, then read back what it wrote.

    The CLI as a subprocess rather than `init_trip.write_trip_dir` imported directly: the service's
    job is to run the CLI, `init` is part of the published interface, and calling the internals
    would let the two drift while the tests stayed green.
    """
    paths = trip_paths(settings, trip_id)
    if not paths.source.is_dir():
        raise ScaffoldError(f"{paths.source} does not exist; materialise the media first")
    if paths.config.exists():
        return ScaffoldResult(config=paths.config, overrides=paths.overrides, created=False)

    argv = [
        settings.story_book_bin,
        "init",
        str(paths.source),
        "--trip-dir",
        str(paths.trip_dir),
        "--trip-name",
        trip_name,
        # Deliberate, and reported by the config `init` writes: without a face model the quality
        # score renormalises and highlights favour scenery over people. The service has no model
        # to point at, and inventing a path would fail at build time instead of here.
        "--no-face-model",
    ]
    if like is not None:
        argv += ["--like", str(like)]

    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise ScaffoldError(
            f"{' '.join(argv)} exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    if not paths.config.exists():
        raise ScaffoldError(f"init reported success but wrote no {paths.config}")

    # The check that matters, and it is one line: the scaffolded corrections file must be loadable
    # *here*, in this trip's context, and must claim nothing about photographs that do not exist.
    scaffolded = Overrides.load(paths.overrides)
    if not scaffolded.is_empty:
        raise ScaffoldError(
            f"{paths.overrides} was scaffolded with corrections in it "
            f"({len(scaffolded.pin)} pin, {len(scaffolded.reject)} reject, "
            f"{len(scaffolded.keeper)} keeper). A starter file naming another trip's photographs "
            "makes the first build fail on a filename this library does not contain."
        )
    return ScaffoldResult(config=paths.config, overrides=paths.overrides, created=True)
