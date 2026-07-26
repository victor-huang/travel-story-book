"""Stage protocol and the result cache that makes an interrupted run resumable.

Two stage shapes:

* `PerItemStage` -- work proportional to media count (hashing, EXIF, scoring, embeddings).
  Split into `compute` (pure, may run in a worker process) and `persist` (parent only, holds
  the DB connection). That split is what lets these stages use a process pool: a sqlite3
  connection cannot cross a process boundary, but a path and a config can.

* `WholeTripStage` -- aggregate work over everything at once (days, events, selection,
  timeline). Runs in the parent with the connection, cached under a single sentinel key.

Every stage carries a `version`. Bumping it invalidates exactly that stage's cache and
nothing else.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from story_book.config import Config
from story_book.db.models import Media

TRIP_SENTINEL = "__trip__"
"""Cache key for whole-trip stages, which have no single media hash."""


class Executor(StrEnum):
    """How the runner should parallelize a per-item stage."""

    SERIAL = "serial"
    PROCESS = "process"
    ASYNC = "async"


class SkipItem(Exception):
    """Raised by `compute` to record a 'skipped' result rather than a failure.

    Skipped is a terminal success state -- the item will not be retried on the next run.
    Use it for "this stage does not apply to this item", not for errors.
    """


@dataclass(slots=True)
class StageContext:
    """Everything a stage needs beyond its input item."""

    conn: sqlite3.Connection
    config: Config
    out_dir: Path
    source_dir: Path
    no_cloud: bool = False

    @property
    def cache_dir(self) -> Path:
        path = self.out_dir / ".cache"
        path.mkdir(parents=True, exist_ok=True)
        return path


class Stage(ABC):
    """Base for every pipeline stage."""

    name: str
    version: int = 1
    description: str = ""

    def available(self, ctx: StageContext) -> tuple[bool, str]:
        """Whether this stage can run. Return (False, reason) to skip it entirely.

        Used for missing system binaries, absent optional dependencies, and `--no-cloud`.
        A skipped stage must never abort the pipeline -- the plan requires that the run
        still completes and exports.
        """
        return True, ""


class PerItemStage(Stage):
    """A stage that processes media items independently."""

    executor: Executor = Executor.SERIAL

    @abstractmethod
    def select(self, ctx: StageContext) -> list[Media]:
        """All items this stage would like to process, before cache filtering."""

    @abstractmethod
    def compute(self, media: Media, config: Config) -> Any:
        """Pure computation. May run in a worker process, so must not touch the DB."""

    @abstractmethod
    def persist(self, ctx: StageContext, media: Media, payload: Any) -> None:
        """Write `compute`'s payload. Runs in the parent process only."""

    async def compute_async(self, media: Media, config: Config) -> Any:
        """Async variant, used when `executor` is ASYNC. Override for network stages."""
        return self.compute(media, config)


class WholeTripStage(Stage):
    """A stage that runs once over the whole trip."""

    @abstractmethod
    def run(self, ctx: StageContext) -> None:
        """Do the work. Owns its own transaction boundaries."""


class BatchStage(Stage):
    """A per-item stage whose work is much cheaper in batches (CLIP, vision APIs).

    The runner hands it groups of items rather than single ones, and records a cache result
    per item in the group so a partial batch still resumes correctly.
    """

    batch_size: int = 32

    @abstractmethod
    def select(self, ctx: StageContext) -> list[Media]:
        """All items this stage would like to process, before cache filtering."""

    @abstractmethod
    def process_batch(self, ctx: StageContext, batch: list[Media]) -> dict[str, Any]:
        """Process a group. Return {media_hash: payload} for items that succeeded.

        Any hash absent from the returned mapping is recorded as failed, so a provider that
        silently drops an image cannot masquerade as success.
        """
