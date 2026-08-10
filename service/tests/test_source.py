"""Materialising a trip's source folder and scaffolding a config over it.

Against **real committed fixture media**, uploaded through a real presigned PUT and fetched back,
because the question this answers is not "did bytes move" but "is what lands a folder
`story-book build` accepts". The last test in this file runs the build. A folder the pipeline cannot
read would pass every byte-count assertion above it.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from storybook_service.app import create_app
from storybook_service.index_sqlite import SqliteIndex
from storybook_service.objectstore import S3ObjectStore
from storybook_service.settings import Settings
from storybook_service.source import trip_paths

from story_book.overrides import Overrides
from tests.conftest import REGION

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "media"

# Two stills with EXIF and GPS, and a clip. Enough for the pipeline to find days and places, which
# is what makes the build a real check rather than a smoke test.
FIXTURE_NAMES = ("heic_gps_offset.heic", "jpeg_gps_no_offset.jpg", "clip_speech.mov")

ME = "traveller@example.com"


@pytest.fixture(scope="session")
def fixture_media() -> dict[str, bytes]:
    """Committed artifacts: assert their presence rather than skipping on a proxy."""
    assert FIXTURES.is_dir(), f"{FIXTURES} is missing; the fixtures are committed"
    media = {}
    for name in FIXTURE_NAMES:
        path = FIXTURES / name
        assert path.is_file(), f"{path} is missing"
        media[name] = path.read_bytes()
    return media


@pytest.fixture
def env(tmp_path, bucket, moto_endpoint):
    settings = Settings(
        data_root=tmp_path / "service-data",
        s3_bucket=bucket,
        s3_region=REGION,
        s3_endpoint_url=moto_endpoint,
    )
    index = SqliteIndex(tmp_path / "index.db")
    app = create_app(settings, index=index, object_store=S3ObjectStore(settings))
    with TestClient(app) as client:
        yield settings, client
    index.close()


def _headers() -> dict[str, str]:
    return {"X-Story-Identity": ME}


def _upload_trip(client, media: dict[str, bytes], *, upload: set[str] | None = None) -> str:
    """Create a trip, negotiate every fixture, and PUT the ones named."""
    trip_id = client.post("/trips", json={"name": "Salzburg"}, headers=_headers()).json()["trip_id"]
    declarations = [
        {"hash": hashlib.blake2b(body).hexdigest(), "filename": name, "size": len(body)}
        for name, body in media.items()
    ]
    response = client.post(
        f"/trips/{trip_id}/assets:negotiate",
        json={"assets": declarations},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    by_hash = {hashlib.blake2b(body).hexdigest(): body for body in media.values()}
    for entry in response.json()["needed"]:
        if upload is not None and entry["filename"] not in upload:
            continue
        put = httpx.put(entry["put_url"], content=by_hash[entry["hash"]], headers=entry["headers"])
        assert put.status_code == 200, put.text
    return trip_id


def _prepare(client, trip_id: str) -> dict:
    response = client.post(f"/trips/{trip_id}/source:prepare", headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()


class TestMaterialise:
    def test_every_uploaded_asset_lands_under_its_original_filename(self, env, fixture_media):
        settings, client = env
        trip_id = _upload_trip(client, fixture_media)
        body = _prepare(client, trip_id)
        assert body["fetched"] == len(FIXTURE_NAMES)
        names = {p.name for p in trip_paths(settings, trip_id).source.iterdir()}
        assert names == set(FIXTURE_NAMES)

    def test_the_bytes_are_the_fixture_bytes(self, env, fixture_media):
        """Identity, not presence. P06 found nine JPEGs under `.mov` names past a schema check."""
        settings, client = env
        trip_id = _upload_trip(client, fixture_media)
        _prepare(client, trip_id)
        source = trip_paths(settings, trip_id).source
        for name, body in fixture_media.items():
            assert (source / name).read_bytes() == body

    def test_the_declared_video_really_is_a_video(self, env, fixture_media):
        settings, client = env
        trip_id = _upload_trip(client, fixture_media)
        _prepare(client, trip_id)
        clip = trip_paths(settings, trip_id).source / "clip_speech.mov"
        described = subprocess.run(
            ["file", "-b", str(clip)], capture_output=True, text=True, check=True
        ).stdout
        assert "ISO Media" in described or "MP4" in described, described

    def test_an_unuploaded_asset_is_reported_missing_rather_than_ignored(self, env, fixture_media):
        settings, client = env
        trip_id = _upload_trip(client, fixture_media, upload={"heic_gps_offset.heic"})
        body = _prepare(client, trip_id)
        assert len(body["missing"]) == 2
        assert body["complete"] is False

    def test_a_complete_trip_reports_nothing_missing(self, env, fixture_media):
        """The control for the test above."""
        _settings, client = env
        body = _prepare(client, _upload_trip(client, fixture_media))
        assert body["missing"] == [] and body["complete"] is True

    def test_preparing_twice_fetches_nothing_the_second_time(self, env, fixture_media):
        """Run it twice. Caching means the second run takes different paths than the first."""
        _settings, client = env
        trip_id = _upload_trip(client, fixture_media)
        _prepare(client, trip_id)
        second = _prepare(client, trip_id)
        assert second["fetched"] == 0
        assert second["already_present"] == len(FIXTURE_NAMES)

    def test_a_truncated_local_file_is_fetched_again(self, env, fixture_media):
        """Size, not existence, is what 'already there' means.

        An interrupted materialisation leaves a short file, and treating presence as completion
        would hand the scanner a truncated photograph -- a wrong result rather than a failure.
        """
        settings, client = env
        trip_id = _upload_trip(client, fixture_media)
        _prepare(client, trip_id)
        victim = trip_paths(settings, trip_id).source / "heic_gps_offset.heic"
        victim.write_bytes(b"xx")
        assert _prepare(client, trip_id)["fetched"] == 1

    def test_a_rename_removes_the_file_left_under_the_old_name(self, env, fixture_media):
        """A second camera's `IMG_0001.JPG` renames both copies, including one already fetched."""
        settings, client = env
        heic = fixture_media["heic_gps_offset.heic"]
        jpeg = fixture_media["jpeg_gps_no_offset.jpg"]
        trip_id = client.post("/trips", json={"name": "Salzburg"}, headers=_headers()).json()[
            "trip_id"
        ]

        first = client.post(
            f"/trips/{trip_id}/assets:negotiate",
            json={
                "assets": [
                    {
                        "hash": hashlib.blake2b(heic).hexdigest(),
                        "filename": "IMG_0001.JPG",
                        "size": len(heic),
                    }
                ]
            },
            headers=_headers(),
        ).json()
        httpx.put(
            first["needed"][0]["put_url"], content=heic, headers=first["needed"][0]["headers"]
        )
        _prepare(client, trip_id)
        assert (trip_paths(settings, trip_id).source / "IMG_0001.JPG").exists()

        second = client.post(
            f"/trips/{trip_id}/assets:negotiate",
            json={
                "assets": [
                    {
                        "hash": hashlib.blake2b(jpeg).hexdigest(),
                        "filename": "IMG_0001.JPG",
                        "size": len(jpeg),
                    }
                ]
            },
            headers=_headers(),
        ).json()
        for entry in second["needed"]:
            httpx.put(entry["put_url"], content=jpeg, headers=entry["headers"])
        body = _prepare(client, trip_id)

        source = trip_paths(settings, trip_id).source
        assert "IMG_0001.JPG" in body["removed"]
        assert not (source / "IMG_0001.JPG").exists()
        assert len(list(source.iterdir())) == 2


class TestScaffold:
    def test_a_complete_trip_is_scaffolded_with_the_cli(self, env, fixture_media):
        settings, client = env
        body = _prepare(client, _upload_trip(client, fixture_media))
        assert body["scaffolded"] is True
        assert Path(body["config"]).is_file()

    def test_the_scaffolded_overrides_load_empty_in_this_trips_context(self, env, fixture_media):
        """A file safe to read is not automatically safe to copy.

        `init` once scaffolded `overrides.example.toml`, whose worked example pins photographs from
        another trip, and the first build it told you to run died on a filename this library does
        not contain. Twelve tests passed; every one asked whether the file was present.
        """
        _settings, client = env
        body = _prepare(client, _upload_trip(client, fixture_media))
        loaded = Overrides.load(Path(body["overrides"]))
        assert loaded.is_empty, loaded

    def test_the_config_records_what_was_measured_from_this_folder(self, env, fixture_media):
        """The reason to run `init` rather than write a config: the numbers come from the media."""
        _settings, client = env
        body = _prepare(client, _upload_trip(client, fixture_media))
        text = Path(body["config"]).read_text()
        assert "measured" in text

    def test_an_incomplete_trip_is_not_scaffolded_and_says_why(self, env, fixture_media):
        """`init` profiles the folder and refuses to overwrite the config it wrote.

        So a config measured from a trip that is two files short is never re-measured -- the guess
        would be permanent.
        """
        _settings, client = env
        trip_id = _upload_trip(client, fixture_media, upload={"heic_gps_offset.heic"})
        body = _prepare(client, trip_id)
        assert body["scaffolded"] is False
        assert "partial" in body["scaffold_detail"]

    def test_an_edited_config_survives_a_later_prepare(self, env, fixture_media):
        _settings, client = env
        trip_id = _upload_trip(client, fixture_media)
        first = _prepare(client, trip_id)
        config = Path(first["config"])
        config.write_text(config.read_text() + "\n# a human edited this\n")
        second = _prepare(client, trip_id)
        assert second["config_created_now"] is False
        assert "a human edited this" in config.read_text()

    def test_the_response_does_not_claim_a_home_coordinate_it_does_not_have(
        self, env, fixture_media
    ):
        """The one place the hosted model is materially weaker, said at the point it applies."""
        _settings, client = env
        body = _prepare(client, _upload_trip(client, fixture_media))
        assert body["home_configured"] is False


class TestTheBuildAccepts:
    """The claim everything else rests on: what ingest produces, the CLI reads."""

    def test_story_book_build_completes_on_the_materialised_folder(self, env, fixture_media):
        settings, client = env
        trip_id = _upload_trip(client, fixture_media)
        body = _prepare(client, trip_id)
        paths = trip_paths(settings, trip_id)

        completed = subprocess.run(
            [
                settings.story_book_bin,
                "build",
                str(paths.source),
                "--out",
                str(paths.out),
                "--config",
                body["config"],
                # No network from a test, and the pipeline must complete without it anyway.
                "--no-cloud",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert (paths.out / "trip.json").is_file()

    def test_the_build_finds_every_asset_that_was_uploaded(self, env, fixture_media):
        """The count is the control: a build that read an empty folder also exits 0."""
        import json

        settings, client = env
        trip_id = _upload_trip(client, fixture_media)
        body = _prepare(client, trip_id)
        paths = trip_paths(settings, trip_id)
        subprocess.run(
            [
                settings.story_book_bin,
                "build",
                str(paths.source),
                "--out",
                str(paths.out),
                "--config",
                body["config"],
                "--no-cloud",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        published = json.loads((paths.out / "trip.json").read_text())
        assert len(published["assets"]) == len(FIXTURE_NAMES)
