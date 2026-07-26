"""Module 7: CLIP image embeddings.

Two consumers depend on this module:

* T23 (dedup) reads the cached `embedding` table and compares vectors by cosine similarity
  (a plain dot product, since every vector this module produces is L2-normalized).
* T13 (quality scoring) calls `ClipRunner.classify` directly for zero-shot content
  classification (screenshot/receipt/document/food/landscape/group/other).

Embeddings are the most expensive local computation in the pipeline, so they are cached by
content hash in the `embedding` table (see `db/schema.sql`) and keyed additionally by model
tag -- switching `models.clip_name`/`clip_pretrained` invalidates the cache for the new tag
without touching rows computed under the old one.

Videos are out of scope here: poster-frame embedding would need a decode step that belongs to
T15, so `EmbeddingStage.select` only ever offers image media to `process_batch`. Nothing in
`open_clip`/`torch` is imported at module load time -- `available()` probes for them so a
build without the `vision` extra still completes, just without embeddings.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from story_book.config import Config
from story_book.db import connection as db
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import BatchStage, StageContext

logger = logging.getLogger(__name__)

_DEFAULT_CLIP_NAME = "ViT-B-32"
_DEFAULT_CLIP_PRETRAINED = "laion2b_s34b_b79k"


def _model_tag(clip_name: str, clip_pretrained: str) -> str:
    return f"{clip_name}/{clip_pretrained}"


CLIP_MODEL_TAG = _model_tag(_DEFAULT_CLIP_NAME, _DEFAULT_CLIP_PRETRAINED)
"""Identifies the default CLIP model in the DB. Actual tag honours `config.models.*`."""


def clip_importable() -> tuple[bool, str]:
    """Whether torch and open_clip are installed, without actually importing (and loading)
    either -- `find_spec` is enough to answer the question and keeps this check near-free.
    """
    import importlib.util

    try:
        missing = [
            name for name in ("torch", "open_clip") if importlib.util.find_spec(name) is None
        ]
    except ImportError:
        missing = ["torch", "open_clip"]
    if missing:
        return False, f"CLIP unavailable: missing {', '.join(missing)}; install the 'vision' extra."
    return True, ""


def _resolve_device(requested: str) -> str:
    """`"auto"` prefers MPS, then CUDA, then CPU; anything else is used verbatim."""
    import torch

    if requested != "auto":
        return requested
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class ClipRunner:
    """One loaded open_clip model, ready to embed images or run zero-shot classification."""

    def __init__(
        self,
        model_tag: str,
        model: Any,
        preprocess: Any,
        tokenizer: Any,
        device: str,
    ) -> None:
        self.model_tag = model_tag
        self.dim = int(model.visual.output_dim)
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer
        self._device = device

    def _load_batch(self, paths: Sequence[Path]) -> Any:
        import torch
        from PIL import Image

        tensors = []
        for path in paths:
            with Image.open(path) as handle:
                tensors.append(self._preprocess(handle.convert("RGB")))
        return torch.stack(tensors).to(self._device)

    def embed_images(self, paths: Sequence[Path]) -> list[list[float]]:
        """L2-normalized image embeddings, one per path, in input order."""
        import torch

        batch = self._load_batch(paths)
        with torch.no_grad():
            features = self._model.encode_image(batch)
            features = features / features.norm(dim=-1, keepdim=True)
        return features.to("cpu").to(torch.float32).tolist()

    def classify(self, paths: Sequence[Path], labels: Sequence[str]) -> list[dict[str, float]]:
        """Zero-shot classification: one dict per path, mapping each label to its probability."""
        import torch

        labels = list(labels)
        image_batch = self._load_batch(paths)
        text_batch = self._tokenizer(labels).to(self._device)
        with torch.no_grad():
            image_features = self._model.encode_image(image_batch)
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            text_features = self._model.encode_text(text_batch)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            logits = 100.0 * image_features @ text_features.T
            probs = logits.softmax(dim=-1).to("cpu").to(torch.float32).tolist()
        return [dict(zip(labels, row, strict=True)) for row in probs]


_clip_cache: dict[tuple[str, str, str], ClipRunner] = {}


def load_clip(config: Config) -> ClipRunner:
    """Load (or return the cached) `ClipRunner` for `config.models.*`."""
    import open_clip

    device = _resolve_device(config.models.device)
    key = (config.models.clip_name, config.models.clip_pretrained, device)
    cached = _clip_cache.get(key)
    if cached is not None:
        return cached

    model, _, preprocess = open_clip.create_model_and_transforms(
        config.models.clip_name, pretrained=config.models.clip_pretrained
    )
    model.eval()
    model.to(device)
    tokenizer = open_clip.get_tokenizer(config.models.clip_name)
    runner = ClipRunner(
        model_tag=_model_tag(config.models.clip_name, config.models.clip_pretrained),
        model=model,
        preprocess=preprocess,
        tokenizer=tokenizer,
        device=device,
    )
    _clip_cache[key] = runner
    return runner


def encode_vector(values: Sequence[float]) -> bytes:
    """Float32 little-endian encoding, for the `embedding.vector` BLOB."""
    return struct.pack(f"<{len(values)}f", *values)


def decode_vector(blob: bytes) -> list[float]:
    """Exact inverse of `encode_vector`."""
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def _cached_hashes(conn: Any, model_tag: str) -> set[str]:
    rows = conn.execute("SELECT media_hash FROM embedding WHERE model = ?", (model_tag,))
    return {row["media_hash"] for row in rows}


def _store_embedding(conn: Any, media_hash: str, model_tag: str, vector: list[float]) -> None:
    conn.execute(
        """
        INSERT INTO embedding (media_hash, model, dim, vector) VALUES (?, ?, ?, ?)
        ON CONFLICT (media_hash) DO UPDATE SET
            model = excluded.model,
            dim = excluded.dim,
            vector = excluded.vector
        """,
        (media_hash, model_tag, len(vector), encode_vector(vector)),
    )


class EmbeddingStage(BatchStage):
    """Compute and cache CLIP embeddings for every image, keyed by content hash and model tag."""

    name = "embeddings"
    version = 1
    description = "CLIP image embeddings for similarity/dedup and zero-shot classification."

    def available(self, ctx: StageContext) -> tuple[bool, str]:
        return clip_importable()

    def select(self, ctx: StageContext) -> list[Media]:
        self.batch_size = ctx.config.models.clip_batch_size
        model_tag = _model_tag(ctx.config.models.clip_name, ctx.config.models.clip_pretrained)
        cached = _cached_hashes(ctx.conn, model_tag)
        return [
            media
            for media in db.iter_media(ctx.conn, kind=str(MediaKind.IMAGE))
            if media.hash not in cached
        ]

    def process_batch(self, ctx: StageContext, batch: list[Media]) -> dict[str, Any]:
        if not batch:
            return {}
        runner = load_clip(ctx.config)
        paths = [Path(media.path) for media in batch]
        vectors = runner.embed_images(paths)
        results: dict[str, Any] = {}
        for media, vector in zip(batch, vectors, strict=True):
            _store_embedding(ctx.conn, media.hash, runner.model_tag, vector)
            results[media.hash] = {"model": runner.model_tag, "dim": runner.dim}
        return results
