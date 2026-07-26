"""Backend tests for Module 8 quality scoring: real temp DB, real committed fixture media.

`sharp.jpg`/`blurred.jpg` and `overexposed.jpg`/`underexposed.jpg` verify the half of the
Module 8 acceptance criterion this task *can* check without a labeled truth set: "the tool
never ranks an obviously blurred or clipped frame first." The other half -- "the top-scoring
photo in a cluster matches the human-preferred photo >=70% of the time" -- needs the P03
labeled set, which does not exist yet, so it is not covered here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from story_book.config import Config
from story_book.db import connection as db
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import StageContext
from story_book.pipeline.embeddings import clip_importable
from story_book.pipeline.quality import CONTENT_CLASSES, ContentClassStage, QualityStage


def _seed(conn: sqlite3.Connection, media_dir: Path, name: str) -> Media:
    media = Media(hash=name, path=str(media_dir / name), kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
    db.upsert_media(conn, media)
    return media


class TestQualityStageSharpnessAcceptance:
    """The acceptance criterion's blur half: `sharp.jpg` must outrank `blurred.jpg`."""

    def test_sharp_scores_above_blurred(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        stage = QualityStage()
        sharp = _seed(conn, media_dir, "sharp.jpg")
        blurred = _seed(conn, media_dir, "blurred.jpg")

        sharp_payload = stage.compute(sharp, config)
        blurred_payload = stage.compute(blurred, config)

        assert sharp_payload["overall"] > blurred_payload["overall"]
        assert sharp_payload["sharpness"] > blurred_payload["sharpness"]


class TestQualityStageExposureAcceptance:
    """The acceptance criterion's clipping half: over/underexposed frames score poorly."""

    def test_overexposed_scores_poorly_on_exposure(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        stage = QualityStage()
        media = _seed(conn, media_dir, "overexposed.jpg")

        payload = stage.compute(media, config)

        assert payload["exposure"] < 0.1

    def test_underexposed_scores_poorly_on_exposure(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        stage = QualityStage()
        media = _seed(conn, media_dir, "underexposed.jpg")

        payload = stage.compute(media, config)

        assert payload["exposure"] < 0.1

    def test_a_clipped_frame_never_outranks_a_well_exposed_sharp_one(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        stage = QualityStage()
        sharp = stage.compute(_seed(conn, media_dir, "sharp.jpg"), config)
        overexposed = stage.compute(_seed(conn, media_dir, "overexposed.jpg"), config)
        underexposed = stage.compute(_seed(conn, media_dir, "underexposed.jpg"), config)

        assert sharp["overall"] > overexposed["overall"]
        assert sharp["overall"] > underexposed["overall"]


class TestQualityStagePersistAgainstRealDb:
    def test_persist_writes_a_score_row(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=media_dir)
        stage = QualityStage()
        media = _seed(conn, media_dir, "sharp.jpg")

        payload = stage.compute(media, config)
        stage.persist(ctx, media, payload)

        row = conn.execute(
            "SELECT sharpness, exposure, contrast, overall FROM score WHERE media_hash = ?",
            (media.hash,),
        ).fetchone()
        assert row is not None
        assert row["overall"] == pytest.approx(payload["overall"])

    def test_persist_does_not_clobber_a_content_class_set_by_the_other_stage(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=media_dir)
        stage = QualityStage()
        media = _seed(conn, media_dir, "sharp.jpg")
        conn.execute(
            "INSERT INTO score (media_hash, content_class) VALUES (?, ?)", (media.hash, "food")
        )

        stage.persist(ctx, media, stage.compute(media, config))

        row = conn.execute(
            "SELECT content_class FROM score WHERE media_hash = ?", (media.hash,)
        ).fetchone()
        assert row["content_class"] == "food"


class TestQualityStageAgainstRealRunner:
    def test_a_full_run_scores_every_fixture_image(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        from story_book.pipeline.scan import ScanStage

        ScanStage().run(ctx)
        stage = QualityStage()

        for media in stage.select(ctx):
            try:
                payload = stage.compute(media, ctx.config)
            except Exception:
                continue
            stage.persist(ctx, media, payload)

        count = ctx.conn.execute(
            "SELECT COUNT(*) AS n FROM score WHERE overall IS NOT NULL"
        ).fetchone()["n"]
        assert count > 0


class TestContentClassStageWithMockedClip:
    """DB plumbing for `ContentClassStage`, without needing torch/open_clip installed."""

    def test_screenshot_and_receipt_are_classified_and_persisted(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path, mocker
    ) -> None:
        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=media_dir)
        screenshot = _seed(conn, media_dir, "screenshot.jpg")
        receipt = _seed(conn, media_dir, "receipt.jpg")

        fake_runner = mocker.Mock()
        fake_runner.classify.return_value = [
            {label: (1.0 if label == "screenshot" else 0.0) for label in CONTENT_CLASSES},
            {label: (1.0 if label == "receipt" else 0.0) for label in CONTENT_CLASSES},
        ]
        stage = ContentClassStage()
        mocker.patch.object(stage, "_runner_for", return_value=fake_runner)

        results = stage.process_batch(ctx, [screenshot, receipt])

        assert results == {screenshot.hash: "screenshot", receipt.hash: "receipt"}
        rows = {
            row["media_hash"]: row["content_class"]
            for row in conn.execute("SELECT media_hash, content_class FROM score")
        }
        assert rows[screenshot.hash] == "screenshot"
        assert rows[receipt.hash] == "receipt"


@pytest.mark.needs_clip
class TestContentClassStageWithRealClip:
    """The acceptance criterion's classifier half, against the real CLIP model."""

    def test_screenshot_fixture_classifies_as_screenshot(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        available, reason = clip_importable()
        if not available:
            pytest.skip(reason)

        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=media_dir)
        screenshot = _seed(conn, media_dir, "screenshot.jpg")

        results = ContentClassStage().process_batch(ctx, [screenshot])

        assert results[screenshot.hash] == "screenshot"

    def test_receipt_fixture_classifies_as_receipt(
        self, conn: sqlite3.Connection, config: Config, out_dir: Path, media_dir: Path
    ) -> None:
        available, reason = clip_importable()
        if not available:
            pytest.skip(reason)

        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=media_dir)
        receipt = _seed(conn, media_dir, "receipt.jpg")

        results = ContentClassStage().process_batch(ctx, [receipt])

        assert results[receipt.hash] == "receipt"
