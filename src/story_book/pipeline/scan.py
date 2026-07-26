"""Module 1: scan the source tree and identify every importable file by content hash.

Nothing exists in the DB yet for a `PerItemStage.select()` to return, so this is a
`WholeTripStage` -- it walks `ctx.source_dir` itself and upserts one `media` row per
importable file. Identity is the BLAKE2b hash of the file's bytes, not its path, which is
what makes re-scanning (or importing the same photo from two folders) a no-op.

Metadata (EXIF dates, GPS) is deliberately left alone here -- that's T11's job. This stage
only records what a directory walk and a hash can tell you: path, kind, size, mtime.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from story_book.db.connection import upsert_media_discovery
from story_book.db.models import Media
from story_book.media_types import IGNORED_NAMES, classify, is_hidden
from story_book.pipeline.base import StageContext, WholeTripStage

logger = logging.getLogger(__name__)

HASH_CHUNK_SIZE = 1024 * 1024


def _iter_candidate_paths(source: Path):
    """Every file under `source`, skipping hidden dot-directories and symlinked directories.

    `followlinks=False` on `os.walk` is what keeps a symlink loop from hanging the scan --
    pruning `dirnames` in place also means we never descend into a hidden directory at all,
    rather than filtering its contents out one by one.
    """
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        root_path = Path(root)
        for filename in sorted(filenames):
            yield root_path / filename


def _hash_file(path: Path) -> str:
    """BLAKE2b of the file's bytes, read in chunks so a large video is never slurped whole."""
    hasher = hashlib.blake2b()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class ScanStage(WholeTripStage):
    """Walk `ctx.source_dir` and upsert one `media` row per importable file.

    Runs on every `build` (`always_run`), because discovery is the one stage whose input set can
    change without any known item changing: a cached result would mean files added to the source
    tree after the first scan are never noticed. Walking the tree and hashing is cheap relative
    to the rest of the pipeline, and the write is idempotent.

    It writes via `upsert_media_discovery`, which touches only the columns the scanner actually
    knows about. Using the full `upsert_media` here would blank the metadata and timezone fields
    on every re-scan -- and with `always_run` that would empty the database on every build,
    since the stages that repopulate them would see cached results and skip.
    """

    name = "scan"
    version = 1
    always_run = True
    description = "Scan the source tree and hash every importable file."

    def run(self, ctx: StageContext) -> None:
        for path in _iter_candidate_paths(ctx.source_dir):
            self._process(ctx, path)

    def _process(self, ctx: StageContext, path: Path) -> None:
        relative = path.relative_to(ctx.source_dir)
        if path.name in IGNORED_NAMES or is_hidden(relative):
            return
        kind = classify(path)
        if kind is None:
            return

        try:
            stat = path.stat()
        except OSError as exc:
            logger.warning("scan: could not stat %s: %s", path, exc)
            return

        try:
            media_hash = _hash_file(path)
        except OSError as exc:
            logger.warning("scan: could not read %s: %s", path, exc)
            return

        media = Media(
            hash=media_hash,
            path=str(path),
            kind=kind,
            bytes=stat.st_size,
            mtime=stat.st_mtime,
        )
        upsert_media_discovery(ctx.conn, media)
