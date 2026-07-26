"""Backend tests for the landmarks stage: real temp DB, real fixture media, mocked provider.

Never touches the network -- the provider handed to `LandmarkStage` is always a stub or a
`MagicMock`. These tests cover the DB-backed halves the unit tests can't: the `selection`-table
dependency on T30, the content-hash + prompt_version cache, and persistence into
`landmark`/`media_landmark`.
"""

from __future__ import annotations

import sqlite3

import pytest

from story_book.config import Config, LandmarkConfig
from story_book.db import connection as db
from story_book.db.models import Media, MediaKind, SelectionScope
from story_book.pipeline.base import StageContext
from story_book.pipeline.landmarks.base import (
    LandmarkIdentification,
    LandmarkImageContext,
    LandmarkStage,
)
from story_book.pipeline.landmarks.providers import ANTHROPIC_API_KEY_ENV


def _seed_selected_media(
    conn: sqlite3.Connection, source_dir, filename: str, media_hash: str, scope: SelectionScope
) -> Media:
    path = source_dir / filename
    media = Media(
        hash=media_hash, path=str(path), kind=MediaKind.IMAGE, bytes=path.stat().st_size, mtime=0.0
    )
    db.upsert_media(conn, media)
    conn.execute(
        "INSERT INTO selection (media_hash, scope, scope_id, rank) VALUES (?, ?, 1, 1)",
        (media_hash, scope.value),
    )
    conn.commit()
    return media


class StubProvider:
    """A `LandmarkProvider` double that never touches the network."""

    name = "stub"

    def __init__(
        self, identification: LandmarkIdentification | None = None, missing: set[str] | None = None
    ):
        self._identification = identification or LandmarkIdentification(
            name="Belvedere Palace",
            confidence=0.9,
            description="A baroque palace.",
            notable_feature="The Kiss",
        )
        self._missing = missing or set()
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return True, ""

    def estimate_cost(self, image_count: int, images_per_request: int):
        from story_book.pipeline.landmarks.base import CostEstimate

        return CostEstimate(
            request_count=max(1, image_count), estimated_usd=0.0, model="stub-model"
        )

    def identify(self, batch: list[LandmarkImageContext]) -> dict[str, LandmarkIdentification]:
        self.calls += 1
        return {
            ctx.media.hash: self._identification
            for ctx in batch
            if ctx.media.hash not in self._missing
        }


class TestSelectReadsFromSelectionTable:
    def test_empty_selection_table_yields_no_candidates(self, ctx: StageContext) -> None:
        stage = LandmarkStage(provider_factory=lambda cfg: StubProvider())
        assert stage.select(ctx) == []

    def test_only_cluster_and_event_scopes_are_candidates(
        self, ctx: StageContext, source_dir
    ) -> None:
        _seed_selected_media(ctx.conn, source_dir, "sharp.jpg", "h_cluster", SelectionScope.CLUSTER)
        _seed_selected_media(ctx.conn, source_dir, "blurred.jpg", "h_event", SelectionScope.EVENT)
        _seed_selected_media(ctx.conn, source_dir, "receipt.jpg", "h_day", SelectionScope.DAY)

        stage = LandmarkStage(provider_factory=lambda cfg: StubProvider())
        candidates = {m.hash for m in stage.select(ctx)}

        assert candidates == {"h_cluster", "h_event"}


class TestProcessBatchPersists:
    def test_landmark_and_media_landmark_rows_are_written(
        self, ctx: StageContext, source_dir
    ) -> None:
        media = _seed_selected_media(
            ctx.conn, source_dir, "sharp.jpg", "h0", SelectionScope.CLUSTER
        )
        stage = LandmarkStage(provider_factory=lambda cfg: StubProvider())

        results = stage.process_batch(ctx, [media])

        assert "h0" in results
        landmark = ctx.conn.execute("SELECT * FROM landmark WHERE source = 'stub'").fetchone()
        assert landmark["name"] == "Belvedere Palace"
        assert landmark["confidence"] == pytest.approx(0.9)
        assert "The Kiss" in landmark["description"]
        assert landmark["prompt_version"] == ctx.config.landmarks.prompt_version
        link = ctx.conn.execute(
            "SELECT * FROM media_landmark WHERE media_hash = 'h0' AND landmark_id = ?",
            (landmark["id"],),
        ).fetchone()
        assert link is not None

    def test_provider_dropping_an_image_excludes_it_from_the_results(
        self, ctx: StageContext, source_dir
    ) -> None:
        m0 = _seed_selected_media(ctx.conn, source_dir, "sharp.jpg", "h0", SelectionScope.CLUSTER)
        m1 = _seed_selected_media(ctx.conn, source_dir, "blurred.jpg", "h1", SelectionScope.CLUSTER)
        stage = LandmarkStage(provider_factory=lambda cfg: StubProvider(missing={"h1"}))

        results = stage.process_batch(ctx, [m0, m1])

        assert set(results) == {"h0"}
        assert (
            ctx.conn.execute("SELECT * FROM media_landmark WHERE media_hash = 'h1'").fetchone()
            is None
        )


class TestCachingByContentHashAndPromptVersion:
    def test_second_run_makes_no_provider_call(self, ctx: StageContext, source_dir) -> None:
        media = _seed_selected_media(
            ctx.conn, source_dir, "sharp.jpg", "h0", SelectionScope.CLUSTER
        )
        provider = StubProvider()
        stage = LandmarkStage(provider_factory=lambda cfg: provider)

        stage.process_batch(ctx, [media])
        assert provider.calls == 1

        # A fresh select() call should now find nothing left to do -- the media_landmark join
        # already has an entry at this prompt_version.
        assert stage.select(ctx) == []

    def test_bumping_prompt_version_invalidates_the_cache(
        self, ctx: StageContext, source_dir
    ) -> None:
        media = _seed_selected_media(
            ctx.conn, source_dir, "sharp.jpg", "h0", SelectionScope.CLUSTER
        )
        provider = StubProvider()
        stage = LandmarkStage(provider_factory=lambda cfg: provider)
        stage.process_batch(ctx, [media])
        assert stage.select(ctx) == []

        bumped = Config(
            landmarks=LandmarkConfig(
                provider="stub", images_per_request=4, max_requests=400, prompt_version=2
            )
        )
        bumped_ctx = StageContext(
            conn=ctx.conn, config=bumped, out_dir=ctx.out_dir, source_dir=ctx.source_dir
        )

        candidates = stage.select(bumped_ctx)

        assert [m.hash for m in candidates] == ["h0"]


class TestMaxRequestsHit:
    def test_dropped_items_are_logged_and_excluded(
        self, ctx: StageContext, source_dir, caplog: pytest.LogCaptureFixture
    ) -> None:
        for i, filename in enumerate(["sharp.jpg", "blurred.jpg", "receipt.jpg"]):
            _seed_selected_media(ctx.conn, source_dir, filename, f"h{i}", SelectionScope.CLUSTER)
        capped_config = Config(
            landmarks=LandmarkConfig(provider="stub", images_per_request=1, max_requests=1)
        )
        capped_ctx = StageContext(
            conn=ctx.conn, config=capped_config, out_dir=ctx.out_dir, source_dir=ctx.source_dir
        )
        stage = LandmarkStage(provider_factory=lambda cfg: StubProvider())

        with caplog.at_level("WARNING"):
            candidates = stage.select(capped_ctx)

        assert len(candidates) == 1
        assert any("max_requests" in record.message for record in caplog.records)


class TestNoCloudSkipsEntirely:
    def test_available_is_false_and_pipeline_can_still_complete(self, ctx: StageContext) -> None:
        no_cloud_ctx = StageContext(
            conn=ctx.conn,
            config=ctx.config,
            out_dir=ctx.out_dir,
            source_dir=ctx.source_dir,
            no_cloud=True,
        )
        stage = LandmarkStage(provider_factory=lambda cfg: StubProvider())

        available, reason = stage.available(no_cloud_ctx)

        assert available is False
        assert reason == "--no-cloud"


class TestNoKeyConfigured:
    def test_real_anthropic_provider_without_a_key_is_a_clean_skip(
        self, ctx: StageContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
        real_provider_ctx = StageContext(
            conn=ctx.conn,
            config=Config(landmarks=LandmarkConfig(provider="anthropic")),
            out_dir=ctx.out_dir,
            source_dir=ctx.source_dir,
        )

        available, reason = LandmarkStage().available(real_provider_ctx)

        assert available is False
        assert ANTHROPIC_API_KEY_ENV in reason


class TestCostEstimatePrintedBeforeAnyCall:
    def test_estimate_is_printed_and_no_identify_call_happens_during_select(
        self, ctx: StageContext, source_dir, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_selected_media(ctx.conn, source_dir, "sharp.jpg", "h0", SelectionScope.CLUSTER)
        provider = StubProvider()
        stage = LandmarkStage(provider_factory=lambda cfg: provider)

        stage.select(ctx)

        assert provider.calls == 0
        assert "estimated cost" in capsys.readouterr().out
