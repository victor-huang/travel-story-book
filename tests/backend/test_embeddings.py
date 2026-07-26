"""Real-model coverage for `pipeline/embeddings.py`.

Model load is slow (~10-15s the first time, cached by `open_clip`/`torch` afterwards), so the
real `ClipRunner` is built once per module via a module-scoped fixture and reused across every
test below. All of these are marked `needs_clip` and skip cleanly when torch/open_clip aren't
importable, per `clip_importable()`.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pytest

from story_book.config import Config
from story_book.db import connection as db
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import StageContext
from story_book.pipeline.embeddings import (
    ClipRunner,
    EmbeddingStage,
    clip_importable,
    decode_vector,
    load_clip,
)

pytestmark = pytest.mark.needs_clip


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@pytest.fixture(scope="module")
def clip_runner() -> ClipRunner:
    available, reason = clip_importable()
    if not available:
        pytest.skip(reason)
    return load_clip(Config())


class TestClipRunnerEmbedImages:
    def test_returns_one_l2_normalized_vector_per_path(self, clip_runner: ClipRunner, media_dir):
        vectors = clip_runner.embed_images([media_dir / "sharp.jpg", media_dir / "blurred.jpg"])
        assert len(vectors) == 2
        for vector in vectors:
            assert len(vector) == clip_runner.dim
            assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, abs_tol=1e-3)

    def test_burst_pair_scores_higher_than_distinct_pair(self, clip_runner: ClipRunner, media_dir):
        burst_a, burst_b, distinct_a, distinct_b = clip_runner.embed_images(
            [
                media_dir / "burst_a.jpg",
                media_dir / "burst_b.jpg",
                media_dir / "distinct_a.jpg",
                media_dir / "distinct_b.jpg",
            ]
        )
        burst_similarity = _cosine(burst_a, burst_b)
        distinct_similarity = _cosine(distinct_a, distinct_b)
        assert burst_similarity > distinct_similarity
        # Not just "higher" -- meaningfully so, per the acceptance criterion.
        assert burst_similarity - distinct_similarity > 0.02

    def test_decoded_stored_vector_matches_the_live_embedding(
        self, clip_runner: ClipRunner, media_dir
    ):
        from story_book.pipeline.embeddings import encode_vector

        (vector,) = clip_runner.embed_images([media_dir / "sharp.jpg"])
        round_tripped = decode_vector(encode_vector(vector))
        assert round_tripped == pytest.approx(vector, abs=1e-6)


class TestClipRunnerClassify:
    def test_probabilities_sum_to_one_per_image(self, clip_runner: ClipRunner, media_dir):
        labels = ["screenshot", "receipt", "document", "food", "landscape", "group", "other"]
        results = clip_runner.classify([media_dir / "screenshot.jpg"], labels)
        assert len(results) == 1
        assert set(results[0]) == set(labels)
        assert math.isclose(sum(results[0].values()), 1.0, abs_tol=1e-3)

    def test_returns_one_result_per_input_path(self, clip_runner: ClipRunner, media_dir):
        results = clip_runner.classify(
            [media_dir / "sharp.jpg", media_dir / "receipt.jpg"], ["photo", "receipt"]
        )
        assert len(results) == 2


class TestEmbeddingStageAgainstRealFixtures:
    def _seed_media(self, conn: sqlite3.Connection, media_dir: Path, name: str) -> Media:
        media = Media(
            hash=name,
            path=str(media_dir / name),
            kind=MediaKind.IMAGE,
            bytes=1,
            mtime=0.0,
        )
        db.upsert_media(conn, media)
        return media

    def test_process_batch_caches_embeddings_in_the_db(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=media_dir)
        media = self._seed_media(conn, media_dir, "sharp.jpg")

        stage = EmbeddingStage()
        results = stage.process_batch(ctx, [media])

        assert media.hash in results
        row = conn.execute(
            "SELECT model, dim, vector FROM embedding WHERE media_hash = ?", (media.hash,)
        ).fetchone()
        assert row is not None
        assert row["dim"] == len(decode_vector(row["vector"]))

    def test_rerun_recomputes_nothing_once_cached(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=media_dir)
        self._seed_media(conn, media_dir, "sharp.jpg")

        stage = EmbeddingStage()
        first_pending = stage.select(ctx)
        stage.process_batch(ctx, first_pending)

        second_pending = stage.select(ctx)

        assert first_pending != []
        assert second_pending == []
