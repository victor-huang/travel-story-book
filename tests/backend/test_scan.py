"""Backend tests for the scan stage: real temp DB, real committed fixture media."""

from __future__ import annotations

from pathlib import Path

from story_book.db import connection as db
from story_book.pipeline.base import StageContext
from story_book.pipeline.scan import ScanStage


class TestScanTwice:
    """The Module 1 acceptance criterion: scanning the real trip twice adds no new rows."""

    def test_scanning_twice_produces_zero_new_rows(self, ctx: StageContext) -> None:
        ScanStage().run(ctx)
        first_count = db.count_media(ctx.conn)

        ScanStage().run(ctx)
        second_count = db.count_media(ctx.conn)

        assert second_count == first_count

    def test_scanning_finds_at_least_one_row(self, ctx: StageContext) -> None:
        ScanStage().run(ctx)
        assert db.count_media(ctx.conn) > 0


class TestContentHashDeduplication:
    """The payoff of hashing bytes instead of trusting the path: byte-identical files under
    different names collapse to one `media` row.
    """

    def test_byte_identical_fixtures_collapse_to_one_row(self, ctx: StageContext) -> None:
        ScanStage().run(ctx)
        exact_a = (ctx.source_dir / "exact_a.jpg").read_bytes()
        exact_b = (ctx.source_dir / "exact_b.jpg").read_bytes()
        assert exact_a == exact_b, "fixture precondition: exact_a/exact_b must be byte-identical"

        import hashlib

        digest = hashlib.blake2b(exact_a).hexdigest()
        row = ctx.conn.execute(
            "SELECT COUNT(*) AS n FROM media WHERE hash = ?", (digest,)
        ).fetchone()
        assert row["n"] == 1

    def test_distinct_fixtures_produce_distinct_rows(self, ctx: StageContext) -> None:
        ScanStage().run(ctx)
        distinct_a_hash = _hash_of(ctx.source_dir / "distinct_a.jpg")
        distinct_b_hash = _hash_of(ctx.source_dir / "distinct_b.jpg")
        assert distinct_a_hash != distinct_b_hash

        rows = {
            row["hash"]
            for row in ctx.conn.execute(
                "SELECT hash FROM media WHERE hash IN (?, ?)",
                (distinct_a_hash, distinct_b_hash),
            )
        }
        assert rows == {distinct_a_hash, distinct_b_hash}


class TestRecordedFields:
    def test_path_bytes_kind_and_mtime_are_recorded(self, ctx: StageContext) -> None:
        ScanStage().run(ctx)
        sharp = ctx.source_dir / "sharp.jpg"
        expected_hash = _hash_of(sharp)

        row = ctx.conn.execute("SELECT * FROM media WHERE hash = ?", (expected_hash,)).fetchone()
        assert row is not None
        assert row["path"] == str(sharp)
        assert row["bytes"] == sharp.stat().st_size
        assert row["kind"] == "image"
        assert row["mtime"] == sharp.stat().st_mtime

    def test_video_fixtures_are_recorded_as_video_kind(self, ctx: StageContext) -> None:
        ScanStage().run(ctx)
        clip_hash = _hash_of(ctx.source_dir / "clip_silent.mp4")
        row = ctx.conn.execute("SELECT kind FROM media WHERE hash = ?", (clip_hash,)).fetchone()
        assert row["kind"] == "video"

    def test_metadata_fields_are_left_untouched(self, ctx: StageContext) -> None:
        """Dates and GPS are T11's job -- scan must not populate them."""
        ScanStage().run(ctx)
        rows = ctx.conn.execute("SELECT taken_utc, lat, lon FROM media").fetchall()
        assert rows, "expected at least one scanned row"
        assert all(r["taken_utc"] is None and r["lat"] is None and r["lon"] is None for r in rows)


class TestNonMediaIsIgnored:
    def test_sidecar_text_file_is_not_imported(self, ctx: StageContext) -> None:
        ScanStage().run(ctx)
        rows = ctx.conn.execute("SELECT path FROM media").fetchall()
        assert not any(row["path"].endswith("notes.txt") for row in rows)


class TestNonDestructive:
    """The pipeline's hard guarantee: scanning never modifies the source tree."""

    def test_source_files_are_unmodified_after_scan(self, ctx: StageContext) -> None:
        before = {
            p: (p.stat().st_size, p.read_bytes())
            for p in sorted(ctx.source_dir.rglob("*"))
            if p.is_file()
        }

        ScanStage().run(ctx)

        after = {
            p: (p.stat().st_size, p.read_bytes())
            for p in sorted(ctx.source_dir.rglob("*"))
            if p.is_file()
        }
        assert before == after

    def test_no_new_files_are_created_in_source(self, ctx: StageContext) -> None:
        before = {p for p in ctx.source_dir.rglob("*") if p.is_file()}
        ScanStage().run(ctx)
        after = {p for p in ctx.source_dir.rglob("*") if p.is_file()}
        assert before == after


class TestUnreadableFileDoesNotAbortRun:
    def test_a_permission_denied_file_is_skipped_and_the_run_completes(
        self, ctx: StageContext
    ) -> None:
        blocked = ctx.source_dir / "blocked.jpg"
        blocked.write_bytes(b"unreadable")
        blocked.chmod(0o000)
        try:
            ScanStage().run(ctx)  # must not raise
        finally:
            blocked.chmod(0o644)

        rows = ctx.conn.execute("SELECT path FROM media").fetchall()
        assert not any(row["path"] == str(blocked) for row in rows)


def _hash_of(path: Path) -> str:
    import hashlib

    return hashlib.blake2b(path.read_bytes()).hexdigest()
