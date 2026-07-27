"""Derived images: a thumbnail and a preview per photo.

The report and the package both need pixels, and neither is allowed to read the database. So the
pixels have to exist as files that `trip.json` can point at, which is what this stage produces --
two long-edge-bounded JPEGs per photo, cached by content hash like any other per-item work.

Three decisions, each forced by a contract elsewhere:

* **Its own stage, not part of the timeline.** Decoding thousands of originals is expensive, and
  the timeline is `always_run`. As a `PerItemStage` this is cached per item, so an interrupt costs
  one photo instead of the whole library.
* **`compute` returns bytes; `persist` writes them.** `compute` may run in a worker process and is
  handed only `(media, config)` -- it has no `out_dir` and no connection. Encoding in the worker
  and writing in the parent keeps the parallelism without leaking the output path into config.
* **Videos are skipped here.** A clip's thumbnail is its poster frame, which the video stage has
  already extracted and recorded in `video_meta`. Decoding the container again to produce a second
  copy would be pure waste, so the timeline points at the poster instead.

Filenames derive from the content hash, not from the `asset_id`: `asset_id` is a prefix whose
length is decided at timeline time and can grow if two hashes collide, and a file whose name
changes because an unrelated photo was added is not a cache.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from story_book.config import Config
from story_book.db import connection as db
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import Executor, PerItemStage, SkipItem, StageContext

logger = logging.getLogger(__name__)

THUMBS_DIRNAME = "thumbs"
PREVIEWS_DIRNAME = "previews"

# The content hash is 128 hex characters. The first 16 are unique across any realistic trip and
# keep a directory listing readable.
NAME_LENGTH = 16


def derived_name(media_hash: str) -> str:
    return f"{media_hash[:NAME_LENGTH]}.jpg"


def thumbnail_relpath(media_hash: str) -> str:
    return f"{THUMBS_DIRNAME}/{derived_name(media_hash)}"


def preview_relpath(media_hash: str) -> str:
    return f"{PREVIEWS_DIRNAME}/{derived_name(media_hash)}"


@dataclass(slots=True)
class DerivedImages:
    thumbnail: bytes
    preview: bytes


def _encode(image: Image.Image, long_edge: int, quality: int) -> bytes:
    copy = image.copy()
    copy.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    copy.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


class ThumbnailStage(PerItemStage):
    """Write a thumbnail and a preview per photo, so the outputs never need the originals."""

    name = "thumbnails"
    version = 1
    description = "Bounded-size JPEG derivatives for the report and the package."
    executor = Executor.PROCESS

    def select(self, ctx: StageContext) -> list[Media]:
        return list(db.iter_media(ctx.conn, kind=str(MediaKind.IMAGE)))

    def compute(self, media: Media, config: Config) -> DerivedImages:
        source = Path(media.path)
        if not source.exists():
            raise SkipItem(f"source missing: {source}")
        with Image.open(source) as image:
            rgb = image.convert("RGB")
            return DerivedImages(
                thumbnail=_encode(
                    rgb, config.report.thumbnail_long_edge, config.report.jpeg_quality
                ),
                preview=_encode(rgb, config.report.preview_long_edge, config.report.jpeg_quality),
            )

    def persist(self, ctx: StageContext, media: Media, payload: DerivedImages) -> None:
        for relpath, data in (
            (thumbnail_relpath(media.hash), payload.thumbnail),
            (preview_relpath(media.hash), payload.preview),
        ):
            target = ctx.out_dir / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
