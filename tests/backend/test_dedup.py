"""Dedup against a real temp DB and the committed fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from story_book.config import DedupConfig
from story_book.db import connection as db
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import StageContext
from story_book.pipeline.days import DaysStage
from story_book.pipeline.dedup import DedupStage, PhashStage, hamming, phash
from story_book.pipeline.events import EventStage

VIENNA = (48.2082, 16.3738)


def _seed(ctx: StageContext, media_dir: Path, filename: str, index: int):
    path = media_dir / filename
    at = datetime(2026, 7, 18, 12) + timedelta(seconds=index)
    media = Media(
        hash=f"h{index:03d}",
        path=str(path),
        kind=MediaKind.IMAGE,
        bytes=path.stat().st_size,
        mtime=path.stat().st_mtime,
        taken_local=at.isoformat(),
        taken_utc=at.isoformat(),
        lat=VIENNA[0],
        lon=VIENNA[1],
    )
    db.upsert_media(ctx.conn, media)
    return media


def _run(ctx: StageContext):
    DaysStage().run(ctx)
    EventStage().run(ctx)
    stage = PhashStage()
    for media in stage.select(ctx):
        stage.persist(ctx, media, stage.compute(media, ctx.config))
    DedupStage().run(ctx)


def _clusters(ctx: StageContext) -> list[set[str]]:
    out = []
    for row in ctx.conn.execute("SELECT id FROM cluster"):
        members = {
            Path(r["path"]).name
            for r in ctx.conn.execute(
                "SELECT m.path FROM media_cluster mc JOIN media m ON m.hash = mc.media_hash "
                "WHERE mc.cluster_id = ?",
                (row["id"],),
            )
        }
        out.append(members)
    return out


class TestPhashOnRealImages:
    def test_byte_identical_files_hash_the_same(self, media_dir: Path) -> None:
        assert phash(str(media_dir / "exact_a.jpg")) == phash(str(media_dir / "exact_b.jpg"))

    def test_a_burst_pair_is_close(self, media_dir: Path) -> None:
        distance = hamming(
            phash(str(media_dir / "burst_a.jpg")), phash(str(media_dir / "burst_b.jpg"))
        )
        assert distance <= DedupConfig().phash_max_distance

    def test_blur_barely_changes_the_hash(self, media_dir: Path) -> None:
        """Low-frequency DCT coefficients survive blur, which is the point of the construction."""
        distance = hamming(
            phash(str(media_dir / "sharp.jpg")), phash(str(media_dir / "blurred.jpg"))
        )
        assert distance <= 4

    def test_heic_can_be_hashed(self, media_dir: Path) -> None:
        assert phash(str(media_dir / "heic_gps_offset.heic")) > 0

    def test_the_hash_fits_a_signed_column(self, media_dir: Path) -> None:
        """A full 64-bit hash overflows SQLite's signed INTEGER on write."""
        assert phash(str(media_dir / "sharp.jpg")) < 2**63

    def test_hashing_is_deterministic(self, media_dir: Path) -> None:
        assert phash(str(media_dir / "sharp.jpg")) == phash(str(media_dir / "sharp.jpg"))


class TestClusteringRealFixtures:
    def test_the_duplicate_pair_clusters(self, ctx: StageContext, media_dir: Path) -> None:
        _seed(ctx, media_dir, "exact_a.jpg", 0)
        _seed(ctx, media_dir, "exact_b.jpg", 1)
        _run(ctx)
        assert {"exact_a.jpg", "exact_b.jpg"} in _clusters(ctx)

    def test_the_burst_pair_clusters(self, ctx: StageContext, media_dir: Path) -> None:
        _seed(ctx, media_dir, "burst_a.jpg", 0)
        _seed(ctx, media_dir, "burst_b.jpg", 1)
        _run(ctx)
        assert {"burst_a.jpg", "burst_b.jpg"} in _clusters(ctx)

    def test_videos_are_never_clustered(self, ctx: StageContext, media_dir: Path) -> None:
        _seed(ctx, media_dir, "sharp.jpg", 0)
        clip = _seed(ctx, media_dir, "sharp.jpg", 1)
        clip.kind = MediaKind.VIDEO
        db.upsert_media(ctx.conn, clip)
        _run(ctx)
        assert all("mov" not in name for members in _clusters(ctx) for name in members)

    def test_a_lone_photo_forms_no_cluster(self, ctx: StageContext, media_dir: Path) -> None:
        _seed(ctx, media_dir, "sharp.jpg", 0)
        _run(ctx)
        assert _clusters(ctx) == []

    def test_nothing_is_deleted(self, ctx: StageContext, media_dir: Path) -> None:
        """Clusters are metadata. A duplicate is selected against, never removed."""
        _seed(ctx, media_dir, "exact_a.jpg", 0)
        _seed(ctx, media_dir, "exact_b.jpg", 1)
        _run(ctx)
        assert db.count_media(ctx.conn) == 2

    def test_keeper_is_left_for_the_selection_stage(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        _seed(ctx, media_dir, "exact_a.jpg", 0)
        _seed(ctx, media_dir, "exact_b.jpg", 1)
        _run(ctx)
        row = ctx.conn.execute("SELECT keeper_hash FROM cluster").fetchone()
        assert row["keeper_hash"] is None


class TestClusteringNeverCrossesAnEvent:
    def test_duplicates_in_different_events_are_not_merged(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        """The constraint that bounds how coarse events may be: a pair split across two events
        can never be found, which is why event detection under-splits."""
        _seed(ctx, media_dir, "exact_a.jpg", 0)
        far = _seed(ctx, media_dir, "exact_b.jpg", 1)
        far.taken_local = "2026-07-18T20:00:00"
        far.taken_utc = "2026-07-18T20:00:00"
        db.upsert_media(ctx.conn, far)
        _run(ctx)
        assert _clusters(ctx) == []


class TestRerun:
    def test_a_rerun_does_not_duplicate_clusters(self, ctx: StageContext, media_dir: Path) -> None:
        _seed(ctx, media_dir, "exact_a.jpg", 0)
        _seed(ctx, media_dir, "exact_b.jpg", 1)
        _run(ctx)
        before = len(_clusters(ctx))
        DedupStage().run(ctx)
        assert len(_clusters(ctx)) == before

    def test_the_stage_is_marked_always_run(self) -> None:
        assert DedupStage().always_run is True

    def test_an_unreadable_image_fails_only_itself(self, ctx: StageContext, tmp_path: Path) -> None:
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not an image")
        media = Media(hash="broken", path=str(broken), kind=MediaKind.IMAGE, bytes=12, mtime=0.0)
        db.upsert_media(ctx.conn, media)
        with pytest.raises(ValueError, match="unreadable image"):
            PhashStage().compute(media, ctx.config)
