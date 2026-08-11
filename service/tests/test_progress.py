"""Reading progress out of a real `story.db`, and refusing to invent any of it.

Everything here runs against a database the actual CLI wrote over the committed fixtures. A mocked
`stage_result` would prove that this module can read a table it also filled in, which is the shape
of test that lets a wrong denominator through.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from storybook_service.progress import read_build_progress
from storybook_service.worker import measure_capability

from story_book.db import connection as db
from story_book.db.models import MediaKind
from story_book.pipeline.base import PerItemStage
from story_book.pipeline.metadata import MetadataStage

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "media"


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, Path, str]:
    """One real `story-book build` over the committed fixtures. Read-only for every test below."""
    assert FIXTURES.is_dir(), f"{FIXTURES} is missing; the fixtures are committed"
    root = tmp_path_factory.mktemp("built")
    source, out = root / "source", root / "out"
    shutil.copytree(FIXTURES, source)
    completed = subprocess.run(
        ["story-book", "build", str(source), "--out", str(out), "--no-cloud"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    capability = json.dumps(
        measure_capability(
            out_dir=out, source_dir=source, config_path=root / "config.toml", no_cloud=True
        )
    )
    return source, out, capability


class TestAFinishedBuild:
    def test_no_stage_has_outstanding_work(self, built):
        source, out, capability = built
        progress = read_build_progress(
            out_dir=out, source_dir=source, config_path=None, capability=capability
        )
        assert progress.stage is None, [(s.name, s.state, s.done, s.total) for s in progress.stages]

    def test_the_media_count_is_the_pipelines_own(self, built):
        source, out, capability = built
        progress = read_build_progress(
            out_dir=out, source_dir=source, config_path=None, capability=capability
        )
        conn = db.connect(out / db.DB_FILENAME, create=False)
        try:
            assert progress.media_known == db.count_media(conn)
        finally:
            conn.close()

    def test_the_video_stages_total_is_the_number_of_videos(self, built):
        """The denominator is the stage's own candidate set, not the library size.

        A stage that applies to three of twenty-five items must read 3/3 when it is done, not
        3/25 -- which would look stuck at 12% for ever.
        """
        source, out, capability = built
        progress = read_build_progress(
            out_dir=out, source_dir=source, config_path=None, capability=capability
        )
        video = next(stage for stage in progress.stages if stage.name == "video")
        conn = db.connect(out / db.DB_FILENAME, create=False)
        try:
            videos = len(list(db.iter_media(conn, kind=str(MediaKind.VIDEO))))
        finally:
            conn.close()
        assert (video.total, video.done, video.state) == (videos, videos, "complete")

    def test_a_whole_trip_stage_counts_one_aggregate_result(self, built):
        source, out, capability = built
        progress = read_build_progress(
            out_dir=out, source_dir=source, config_path=None, capability=capability
        )
        timeline = next(stage for stage in progress.stages if stage.name == "timeline")
        assert (timeline.total, timeline.done) == (1, 1)

    def test_quality_counts_a_skipped_item_as_done(self, built):
        """`skipped` is terminal success -- three fixtures are videos the quality stage skips.

        Counting only `ok` would leave this stage permanently short of its total and pin the
        reported stage to it.
        """
        source, out, capability = built
        progress = read_build_progress(
            out_dir=out, source_dir=source, config_path=None, capability=capability
        )
        quality = next(stage for stage in progress.stages if stage.name == "quality")
        assert quality.done == quality.total and quality.state == "complete"

    def test_no_percentage_is_published(self, built):
        """The one thing I22 forbids. Asserted on the object, so adding one fails here."""
        source, out, capability = built
        progress = read_build_progress(
            out_dir=out, source_dir=source, config_path=None, capability=capability
        )
        fields = set(progress.__slots__) | set(progress.stages[0].__slots__)
        assert not {"percent", "percentage", "fraction", "eta", "seconds_remaining"} & fields


class TestADegradedDeployment:
    def test_an_unavailable_stage_is_named_with_the_stages_own_reason(self, built):
        """Open question 18's requirement: a degraded build must report that it was degraded."""
        source, out, capability = built
        measured = json.loads(capability)
        assert measured["unavailable"], "this deployment ran every stage; nothing to assert"
        progress = read_build_progress(
            out_dir=out, source_dir=source, config_path=None, capability=capability
        )
        by_name = {stage.name: stage for stage in progress.stages}
        for entry in measured["unavailable"]:
            reported = by_name[entry["stage"]]
            assert (reported.state, reported.total, reported.detail) == (
                "unavailable",
                None,
                entry["reason"],
            )

    def test_an_unavailable_stage_does_not_become_the_current_stage(self, built):
        """Otherwise a `clip`-less image reports `embeddings` for the rest of the run."""
        source, out, capability = built
        measured = json.loads(capability)
        names = {entry["stage"] for entry in measured["unavailable"]}
        progress = read_build_progress(
            out_dir=out, source_dir=source, config_path=None, capability=capability
        )
        assert progress.stage not in names

    def test_without_the_measurement_nothing_is_claimed_about_availability(self, built):
        """The control: with no capability recorded, no stage is reported as unavailable.

        An absent measurement must not be reported as a measurement of absence.
        """
        source, out, _ = built
        progress = read_build_progress(out_dir=out, source_dir=source, config_path=None)
        assert not [stage for stage in progress.stages if stage.state == "unavailable"]


class TestBeforeThereIsADatabase:
    def test_a_missing_story_db_reports_no_measurement_rather_than_zero(self, tmp_path):
        progress = read_build_progress(
            out_dir=tmp_path / "out", source_dir=tmp_path / "source", config_path=None
        )
        assert (progress.stage, progress.total, progress.stages_total) == (None, None, 0)
        assert "has not opened its database" in progress.detail


class TestAnEmptyLibraryIsNotEvidence:
    """Found by reading a real response one second into a real build, not by a test.

    Before `scan` commits, `media` is empty, every per-item stage selects nothing, and the first
    version of this module reported five of eighteen stages **complete** with the words "does not
    apply to this library". A client would have drawn a bar a quarter full over a build that had not
    read a single photograph.
    """

    def test_before_the_scan_no_stage_claims_to_be_complete(self, tmp_path):
        out = tmp_path / "out"
        # The database as `story-book build` leaves it in its first moment: schema, no media.
        db.connect(out / db.DB_FILENAME).close()
        progress = read_build_progress(
            out_dir=out, source_dir=tmp_path / "source", config_path=None
        )
        assert progress.media_known == 0
        assert progress.stages_complete == 0, [
            (s.name, s.state, s.total) for s in progress.stages if s.state == "complete"
        ]

    def test_the_first_stage_is_the_one_that_discovers_the_media(self, tmp_path):
        out = tmp_path / "out"
        db.connect(out / db.DB_FILENAME).close()
        progress = read_build_progress(
            out_dir=out, source_dir=tmp_path / "source", config_path=None
        )
        assert (progress.stage, progress.stage_index) == ("scan", 1)

    def test_a_stage_that_genuinely_does_not_apply_still_reads_complete(self, built):
        """The control, and it is why the fix is conditioned on the media count rather than removed.

        Once the library *has* been scanned, a stage with no candidates really is done -- the
        Once scanned, a stage with no candidates really is done. Here the whole fixture set is
        built, so this asserts the other branch of that condition is reachable.
        """
        source, out, capability = built
        progress = read_build_progress(
            out_dir=out, source_dir=source, config_path=None, capability=capability
        )
        assert progress.media_known > 0
        assert all(stage.state != "pending" for stage in progress.stages), [
            (s.name, s.state) for s in progress.stages if s.state == "pending"
        ]


class TestTheDenominatorCannotShrink:
    """The bug this guards is real and lives upstream, in `EmbeddingStage`.

    Its `select()` filters out items it has already embedded -- it has to, because the cache key
    carries no model tag -- so `len(select())` walks *down* as the stage progresses. A total taken
    straight from `select()` would converge on `done` from above, and the stage would report 100%
    while half the work was outstanding.
    """

    def test_a_self_filtering_stage_keeps_its_full_total(self, built, mocker):
        source, out, _ = built

        class SelfFiltering(PerItemStage):
            """Named `metadata` so its own cache rows exist in this database and are complete.

            `select()` then returns **nothing**, which is what `EmbeddingStage` does once it has
            embedded everything. Taken literally that is a total of 0.
            """

            name = MetadataStage.name
            version = MetadataStage.version

            def select(self, ctx):
                done = db.completed_hashes(ctx.conn, self.name, self.version)
                return [m for m in db.iter_media(ctx.conn) if m.hash not in done]

            def compute(self, media, config):  # pragma: no cover - never run here
                return None

            def persist(self, ctx, media, payload):  # pragma: no cover - never run here
                return None

        class Plain(SelfFiltering):
            """The control: the same stage without the self-filter, so the totals must agree."""

            def select(self, ctx):
                return list(db.iter_media(ctx.conn))

        mocker.patch(
            "storybook_service.progress.build_stages", return_value=[SelfFiltering(), Plain()]
        )
        progress = read_build_progress(out_dir=out, source_dir=source, config_path=None)
        filtered, plain = progress.stages

        conn = db.connect(out / db.DB_FILENAME, create=False)
        try:
            ctx = mocker.MagicMock(conn=conn)
            naive = len(SelfFiltering().select(ctx))
        finally:
            conn.close()
        # The two halves of the claim: the naive denominator is empty, and the published one is not.
        assert naive == 0
        assert filtered.total == plain.total > 0
