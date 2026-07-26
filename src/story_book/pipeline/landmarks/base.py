"""Module 11: landmark identification -- provider interface and pipeline stage.

This is the one cloud-coupled stage in the pipeline; everything else runs on-machine. Only
images already chosen by selection (T30's event highlights and cluster keepers -- a few
hundred per trip, not thousands) are ever sent anywhere, and only when a provider and API key
are configured and `--no-cloud` is not set. `LandmarkProvider` is a thin seam so the one
vendor-coupled piece of the codebase stays swappable; concrete implementations live in
`providers.py`.

T30 (selection) does not exist yet at the time this stage was written. `select()` reads the
`selection` table directly and returns an empty list when it is empty, which is the correct
behavior today and will start returning real candidates the moment T30 lands -- no change
needed here.
"""

from __future__ import annotations

import logging
import mimetypes
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from story_book.config import LandmarkConfig
from story_book.db import connection as db
from story_book.db.models import Media
from story_book.pipeline.base import BatchStage, StageContext

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LandmarkImageContext:
    """One image plus the geographic context that turns "a palace" into a named landmark."""

    media: Media
    image_bytes: bytes
    media_type: str
    place_label: str | None


@dataclass(slots=True)
class LandmarkIdentification:
    """A provider's structured answer for one image."""

    name: str
    confidence: float
    description: str
    notable_feature: str | None = None


@dataclass(slots=True)
class CostEstimate:
    request_count: int
    estimated_usd: float
    model: str


class LandmarkProvider(ABC):
    """Vendor seam. One instance is built per run from `LandmarkConfig`."""

    name: str

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """Whether this provider can run right now (e.g. an API key is configured)."""

    @abstractmethod
    def estimate_cost(self, image_count: int, images_per_request: int) -> CostEstimate:
        """Estimate the request count and USD cost for identifying `image_count` images."""

    @abstractmethod
    def identify(self, batch: list[LandmarkImageContext]) -> dict[str, LandmarkIdentification]:
        """One provider request covering `batch`.

        Returns `{media_hash: identification}` for images the provider actually identified.
        An image missing from the mapping -- dropped by the provider, or present but
        malformed -- is the caller's cue to mark that item failed, never to crash.
        """


ConfirmFn = Callable[[str], bool]
ProviderFactory = Callable[[LandmarkConfig], "LandmarkProvider | None"]


def _prompt_confirm(message: str) -> bool:
    """Default confirmation gate: ask on stdin. EOF (non-interactive) is treated as decline."""
    try:
        reply = input(f"{message} Proceed? [y/N] ")
    except EOFError:
        return False
    return reply.strip().lower() in {"y", "yes"}


def _default_provider_factory(config: LandmarkConfig) -> LandmarkProvider | None:
    # Deferred import: `providers.py` imports this module at load time, so importing it back
    # here at module scope would be circular. Importing inside the function breaks the cycle
    # without needing a lazy-loading trick anywhere else.
    from story_book.pipeline.landmarks import providers

    return providers.get_provider(config)


def cap_to_max_requests(
    candidates: list[Media], config: LandmarkConfig
) -> tuple[list[Media], list[Media]]:
    """Split `candidates` into (kept, dropped) so at most `max_requests` requests are made.

    Pure and DB-free so the ceiling logic is unit-testable without a database.
    """
    max_images = max(config.max_requests, 0) * max(config.images_per_request, 1)
    if len(candidates) <= max_images:
        return candidates, []
    return candidates[:max_images], candidates[max_images:]


def estimate_and_confirm(
    candidates: list[Media],
    config: LandmarkConfig,
    provider: LandmarkProvider,
    confirm: ConfirmFn,
) -> CostEstimate | None:
    """Print the estimated request count and cost, and gate above the configured threshold.

    Returns the estimate if the run should proceed, `None` if the user declined. The estimate
    is always printed before any provider call is made, regardless of whether confirmation is
    required -- this is what the T25 acceptance criterion means by "printed before any call".
    """
    estimate = provider.estimate_cost(len(candidates), config.images_per_request)
    print(
        f"landmarks: {estimate.request_count} request(s) to '{provider.name}' "
        f"({estimate.model}) covering {len(candidates)} image(s), "
        f"estimated cost ${estimate.estimated_usd:.4f}"
    )
    if estimate.estimated_usd > config.confirm_above_estimated_usd:
        proceed = confirm(
            f"Landmark identification is estimated to cost ${estimate.estimated_usd:.4f} "
            f"over {estimate.request_count} request(s), above the "
            f"${config.confirm_above_estimated_usd:.2f} confirmation threshold."
        )
        if not proceed:
            return None
    return estimate


def _selected_media(conn: sqlite3.Connection, prompt_version: int) -> list[Media]:
    """Event highlights and cluster keepers from the `selection` table, minus anything already
    identified at the current `prompt_version` -- the content-hash + prompt-version cache.

    Returns an empty list when `selection` is empty, which is exactly what happens before T30
    exists.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT s.media_hash
        FROM selection AS s
        WHERE s.scope IN ('cluster', 'event')
          AND NOT EXISTS (
              SELECT 1 FROM media_landmark AS ml
              JOIN landmark AS l ON l.id = ml.landmark_id
              WHERE ml.media_hash = s.media_hash AND l.prompt_version = ?
          )
        """,
        (prompt_version,),
    ).fetchall()
    media: list[Media] = []
    for row in rows:
        item = db.get_media(conn, row["media_hash"])
        if item is not None:
            media.append(item)
    return media


def _place_label(conn: sqlite3.Connection, place_id: int | None) -> str | None:
    if place_id is None:
        return None
    row = conn.execute("SELECT poi, city, country FROM place WHERE id = ?", (place_id,)).fetchone()
    if row is None:
        return None
    parts = [p for p in (row["poi"], row["city"], row["country"]) if p]
    return ", ".join(parts) if parts else None


def _build_context(conn: sqlite3.Connection, media: Media) -> LandmarkImageContext | None:
    """Read the image bytes and its reverse-geocoded place name. `None` on unreadable files --
    that degrades to a failed item for this stage, never a crash for the whole batch."""
    path = Path(media.path)
    try:
        image_bytes = path.read_bytes()
    except OSError:
        logger.warning("landmarks: could not read %s", media.path)
        return None
    media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return LandmarkImageContext(
        media=media,
        image_bytes=image_bytes,
        media_type=media_type,
        place_label=_place_label(conn, media.place_id),
    )


def _describe(identification: LandmarkIdentification) -> str:
    """Fold `notable_feature` into `description` -- the frozen schema has no dedicated column
    for it, and the plan only asks that it be captured, not where."""
    if not identification.notable_feature:
        return identification.description
    return f"{identification.description} Notable feature: {identification.notable_feature}"


def _persist(
    conn: sqlite3.Connection,
    media_hash: str,
    source: str,
    prompt_version: int,
    identification: LandmarkIdentification,
) -> None:
    conn.execute(
        """
        INSERT INTO landmark (name, confidence, description, source, prompt_version)
        VALUES (:name, :confidence, :description, :source, :prompt_version)
        ON CONFLICT (name, source, prompt_version) DO UPDATE SET
            confidence = excluded.confidence,
            description = excluded.description
        """,
        {
            "name": identification.name,
            "confidence": identification.confidence,
            "description": _describe(identification),
            "source": source,
            "prompt_version": prompt_version,
        },
    )
    row = conn.execute(
        "SELECT id FROM landmark WHERE name = ? AND source = ? AND prompt_version = ?",
        (identification.name, source, prompt_version),
    ).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO media_landmark (media_hash, landmark_id) VALUES (?, ?)",
        (media_hash, row["id"]),
    )
    conn.commit()


class LandmarkStage(BatchStage):
    """Identify landmarks in event highlights and cluster keepers via a hosted vision API."""

    name = "landmarks"
    version = 1
    description = "Cloud vision landmark identification (event highlights + cluster keepers)"

    # Runner-level chunk size only. The number of images actually sent per provider request is
    # `config.landmarks.images_per_request`, enforced inside `process_batch` -- `batch_size`
    # just bounds how many candidates the runner hands us at once between DB commits.
    batch_size = 200

    def __init__(
        self,
        confirm: ConfirmFn = _prompt_confirm,
        provider_factory: ProviderFactory | None = None,
    ) -> None:
        self._confirm = confirm
        self._provider_factory = provider_factory or _default_provider_factory

    def available(self, ctx: StageContext) -> tuple[bool, str]:
        if ctx.no_cloud:
            return False, "--no-cloud"
        config = ctx.config.landmarks
        if config.provider == "none":
            return False, "no landmark provider configured (landmarks.provider = 'none')"
        provider = self._provider_factory(config)
        if provider is None:
            return False, f"unknown landmark provider {config.provider!r}"
        return provider.available()

    def select(self, ctx: StageContext) -> list[Media]:
        config = ctx.config.landmarks
        candidates = _selected_media(ctx.conn, config.prompt_version)
        if not candidates:
            return []

        provider = self._provider_factory(config)
        if provider is None:
            return []

        if estimate_and_confirm(candidates, config, provider, self._confirm) is None:
            logger.info("landmarks: user declined the estimated cost; skipping this run")
            return []

        kept, dropped = cap_to_max_requests(candidates, config)
        if dropped:
            logger.warning(
                "landmarks: max_requests=%d caps this run to %d image(s); dropping %d: %s",
                config.max_requests,
                len(kept),
                len(dropped),
                ", ".join(m.hash for m in dropped[:10]) + ("..." if len(dropped) > 10 else ""),
            )
        return kept

    def process_batch(self, ctx: StageContext, batch: list[Media]) -> dict[str, Any]:
        config = ctx.config.landmarks
        provider = self._provider_factory(config)
        if provider is None:
            return {}

        results: dict[str, Any] = {}
        step = max(config.images_per_request, 1)
        for start in range(0, len(batch), step):
            group = batch[start : start + step]
            contexts = [c for c in (_build_context(ctx.conn, m) for m in group) if c is not None]
            if not contexts:
                continue
            identifications = provider.identify(contexts)
            for media_hash, identification in identifications.items():
                _persist(ctx.conn, media_hash, provider.name, config.prompt_version, identification)
                results[media_hash] = identification
        return results
