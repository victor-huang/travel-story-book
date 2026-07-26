"""Shared fixtures.

`tests/unit/` must not touch the DB, filesystem, or network -- use `mocker.patch`.
`tests/backend/` may use the real temp DB and the committed media fixtures.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from story_book.config import Config
from story_book.db import connection as db
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import StageContext

FIXTURE_MEDIA = Path(__file__).parent / "fixtures" / "media"


@pytest.fixture
def media_dir() -> Path:
    """The committed fixture media directory, read-only."""
    if not FIXTURE_MEDIA.exists():
        pytest.skip("fixture media missing -- run `uv run python tests/fixtures/generate.py`")
    return FIXTURE_MEDIA


@pytest.fixture
def source_dir(media_dir: Path, tmp_path: Path) -> Path:
    """A writable copy of the fixture media, for tests that assert non-destructiveness."""
    target = tmp_path / "source"
    shutil.copytree(media_dir, target)
    return target


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def conn(out_dir: Path) -> sqlite3.Connection:
    connection = db.connect(out_dir / db.DB_FILENAME)
    db.ensure_trip(connection, "Test Trip")
    yield connection
    connection.close()


@pytest.fixture
def ctx(conn: sqlite3.Connection, config: Config, out_dir: Path, source_dir: Path) -> StageContext:
    return StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=source_dir)


@pytest.fixture
def make_media():
    """Build a Media with sensible defaults, overriding only what a test cares about."""

    def _make(media_hash: str = "hash0", **overrides) -> Media:
        defaults = {
            "hash": media_hash,
            "path": f"/src/{media_hash}.jpg",
            "kind": MediaKind.IMAGE,
            "bytes": 1024,
            "mtime": 1_700_000_000.0,
        }
        return Media(**{**defaults, **overrides})

    return _make


@pytest.fixture
def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


@pytest.fixture
def has_exiftool() -> bool:
    return shutil.which("exiftool") is not None
