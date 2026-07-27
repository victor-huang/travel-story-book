"""T42: proof that the source tree survives a full build untouched.

This is guarantee #1, and the one whose failure would make the tool actively harmful: it operates
on the only copy of someone's trip. "We never write to the source" is easy to believe and easy to
break -- a stage that opens a file `r+` by accident, an export that moves instead of copies, an
ExifTool invocation without `-overwrite_original` discipline. So it is asserted, not assumed.

The check is a full content-hash manifest of the source tree before and after, plus mtimes and the
directory listing itself, because a file that is rewritten with identical bytes is still a file the
tool had no business writing.
"""

from __future__ import annotations

import os
import shutil
from hashlib import blake2b
from pathlib import Path

import pytest

from story_book.cli import build_stages
from story_book.export.package import ORIGINALS, build_package
from story_book.export.report import render_report
from story_book.pipeline.base import StageContext
from story_book.pipeline.runner import Runner
from story_book.pipeline.timeline import build_timeline


def _tree_manifest(root: Path) -> dict[str, tuple[str, int, float]]:
    """Every file under `root`: content hash, size, and mtime."""
    manifest = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest = blake2b(path.read_bytes()).hexdigest()
        stat = path.stat()
        manifest[str(path.relative_to(root))] = (digest, stat.st_size, stat.st_mtime)
    return manifest


@pytest.fixture
def source_tree(source_dir: Path) -> Path:
    """A writable copy of the fixture media -- writable so a violation *could* happen."""
    assert any(source_dir.iterdir()), "fixture media missing"
    return source_dir


class TestFullBuildLeavesTheSourceUntouched:
    def test_every_file_is_byte_identical_after_a_build(
        self, ctx: StageContext, source_tree: Path
    ) -> None:
        before = _tree_manifest(source_tree)
        Runner(ctx, build_stages(ctx)).run()
        after = _tree_manifest(source_tree)

        changed = {
            name for name in before.keys() & after.keys() if before[name][0] != after[name][0]
        }
        assert changed == set()

    def test_no_file_is_added_to_the_source(self, ctx: StageContext, source_tree: Path) -> None:
        before = set(_tree_manifest(source_tree))
        Runner(ctx, build_stages(ctx)).run()
        assert set(_tree_manifest(source_tree)) - before == set()

    def test_no_file_is_removed_from_the_source(self, ctx: StageContext, source_tree: Path) -> None:
        before = set(_tree_manifest(source_tree))
        Runner(ctx, build_stages(ctx)).run()
        assert before - set(_tree_manifest(source_tree)) == set()

    def test_no_modification_time_changes(self, ctx: StageContext, source_tree: Path) -> None:
        """A rewrite with identical bytes is still a write. mtime catches what a hash cannot."""
        before = _tree_manifest(source_tree)
        Runner(ctx, build_stages(ctx)).run()
        after = _tree_manifest(source_tree)
        touched = {name for name in before if before[name][2] != after[name][2]}
        assert touched == set()

    def test_a_second_build_also_leaves_it_untouched(
        self, ctx: StageContext, source_tree: Path
    ) -> None:
        """Run two takes different paths through the cache, so it gets its own assertion."""
        Runner(ctx, build_stages(ctx)).run()
        before = _tree_manifest(source_tree)
        Runner(ctx, build_stages(ctx)).run()
        assert _tree_manifest(source_tree) == before

    def test_the_source_directory_is_never_opened_for_writing(
        self, ctx: StageContext, source_tree: Path, mocker
    ) -> None:
        """Catches the intent, not just the outcome: an `r+` open that happens to write nothing."""
        real_open = Path.open
        offences: list[tuple[str, str]] = []

        def guarded(self, mode="r", *args, **kwargs):
            if "r" not in mode or "+" in mode:
                try:
                    if source_tree in self.resolve().parents:
                        offences.append((str(self), mode))
                except OSError:  # pragma: no cover - unresolvable path
                    pass
            return real_open(self, mode, *args, **kwargs)

        mocker.patch.object(Path, "open", guarded)
        Runner(ctx, build_stages(ctx)).run()
        assert offences == []


class TestOutputGoesOnlyToTheOutDirectory:
    def test_the_out_directory_is_where_the_work_lands(
        self, ctx: StageContext, source_tree: Path
    ) -> None:
        Runner(ctx, build_stages(ctx)).run()
        assert (ctx.out_dir / "story.db").exists()

    def test_derived_images_land_outside_the_source(
        self, ctx: StageContext, source_tree: Path
    ) -> None:
        Runner(ctx, build_stages(ctx)).run()
        assert not list(source_tree.rglob("thumbs"))


class TestExportsLinkOrCopyButNeverMove:
    def test_an_originals_package_leaves_the_original_in_place(
        self, ctx: StageContext, source_tree: Path
    ) -> None:
        """A move would take the photo out of the user's own library. This is the one that hurts."""
        Runner(ctx, build_stages(ctx)).run()
        document = build_timeline(ctx.conn, ctx.config, None, ctx.out_dir)
        sources = {
            asset["asset_id"]: Path(
                ctx.conn.execute(
                    "SELECT path FROM media WHERE hash = ?", (asset["content_hash"],)
                ).fetchone()["path"]
            )
            for asset in document["assets"].values()
        }
        before = _tree_manifest(source_tree)

        build_package(document, ctx.out_dir, mode=ORIGINALS, source_for=sources)
        assert _tree_manifest(source_tree) == before

    def test_an_exported_original_is_a_hardlink_or_a_copy_not_a_relocation(
        self, ctx: StageContext, source_tree: Path
    ) -> None:
        Runner(ctx, build_stages(ctx)).run()
        document = build_timeline(ctx.conn, ctx.config, None, ctx.out_dir)
        sources = {
            asset["asset_id"]: Path(
                ctx.conn.execute(
                    "SELECT path FROM media WHERE hash = ?", (asset["content_hash"],)
                ).fetchone()["path"]
            )
            for asset in document["assets"].values()
        }
        built = build_package(document, ctx.out_dir, mode=ORIGINALS, source_for=sources)

        exported = [p for day in built.days for p in (day.directory / "full").glob("*")]
        assert exported, "nothing was exported, so this proves nothing"
        for path in exported:
            assert path.exists()
            original = next(
                (s for s in sources.values() if s.name == path.name.split("_", 1)[1]), None
            )
            if original is not None:
                assert original.exists()
                # Same inode means hardlink; different means copy. Either is fine. What must not
                # happen is the original vanishing, which the assertion above already covers.
                assert os.stat(path).st_size == os.stat(original).st_size


class TestTheProofItselfIsSound:
    def test_the_manifest_notices_a_content_change(self, source_tree: Path) -> None:
        """A test that cannot fail proves nothing -- so break the tree on purpose."""
        before = _tree_manifest(source_tree)
        victim = next(p for p in source_tree.rglob("*") if p.is_file())
        victim.write_bytes(victim.read_bytes() + b"tampered")

        assert _tree_manifest(source_tree) != before

    def test_the_manifest_notices_a_deletion(self, source_tree: Path) -> None:
        before = set(_tree_manifest(source_tree))
        victim = next(p for p in source_tree.rglob("*") if p.is_file())
        victim.unlink()

        assert before - set(_tree_manifest(source_tree)) != set()

    def test_the_manifest_notices_an_addition(self, source_tree: Path) -> None:
        before = set(_tree_manifest(source_tree))
        (source_tree / "intruder.txt").write_text("hello")

        assert set(_tree_manifest(source_tree)) - before != set()

    def test_the_write_guard_notices_a_write(self, source_tree: Path, mocker) -> None:
        real_open = Path.open
        offences: list[str] = []

        def guarded(self, mode="r", *args, **kwargs):
            if ("r" not in mode or "+" in mode) and source_tree in self.resolve().parents:
                offences.append(str(self))
            return real_open(self, mode, *args, **kwargs)

        mocker.patch.object(Path, "open", guarded)
        with (source_tree / "probe.txt").open("w") as handle:
            handle.write("x")

        assert offences != []


class TestReportAndPackageDoNotTouchTheSource:
    def test_rendering_the_report_changes_nothing(
        self, ctx: StageContext, source_tree: Path
    ) -> None:
        Runner(ctx, build_stages(ctx)).run()
        before = _tree_manifest(source_tree)
        render_report(build_timeline(ctx.conn, ctx.config, None, ctx.out_dir), ctx.out_dir)
        assert _tree_manifest(source_tree) == before

    def test_deleting_the_whole_output_directory_loses_nothing_of_the_source(
        self, ctx: StageContext, source_tree: Path
    ) -> None:
        """The output is disposable by design; the source is the only irreplaceable thing here."""
        Runner(ctx, build_stages(ctx)).run()
        before = _tree_manifest(source_tree)
        ctx.conn.close()
        shutil.rmtree(ctx.out_dir)
        assert _tree_manifest(source_tree) == before
