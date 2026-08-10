"""The relational index -- the thing `story.db` structurally cannot be.

Tested against the real SQLite implementation rather than a mock, because the properties that
matter here are properties of the schema: uniqueness of an identity, cascade on delete, and
whether a query can be asked for a trip without saying whose it is.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from storybook_service.index import IndexError_, TripAsset, UnknownIndexEngine, for_dsn
from storybook_service.index_sqlite import SqliteIndex, new_id

HASH_A = hashlib.blake2b(b"a").hexdigest()
HASH_B = hashlib.blake2b(b"b").hexdigest()


@pytest.fixture
def index(tmp_path):
    idx = SqliteIndex(tmp_path / "index.db")
    yield idx
    idx.close()


def _asset(media_hash: str, filename: str, size: int = 10) -> TripAsset:
    return TripAsset(
        media_hash=media_hash,
        filename=filename,
        stored_filename=filename,
        size=size,
        declared_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


class TestForDsn:
    def test_a_sqlite_dsn_returns_a_working_index(self, tmp_path):
        idx = for_dsn(f"sqlite:///{tmp_path / 'i.db'}")
        try:
            assert idx.ensure_user(kind="email", value="a@example.com").id
        finally:
            idx.close()

    def test_postgres_is_refused_by_name_rather_than_falling_back(self):
        """The seam, asserted.

        A silent fallback to SQLite on a production host is precisely the failure this interface
        exists to make impossible, and it would look like a working deployment.
        """
        with pytest.raises(UnknownIndexEngine, match="postgresql"):
            for_dsn("postgresql://localhost/story")


class TestEnsureUser:
    def test_a_new_email_creates_a_user(self, index):
        assert index.ensure_user(kind="email", value="a@example.com").id

    def test_the_same_email_returns_the_same_user(self, index):
        first = index.ensure_user(kind="email", value="a@example.com")
        second = index.ensure_user(kind="email", value="a@example.com")
        assert first.id == second.id

    def test_a_different_email_is_a_different_user(self, index):
        """The control for the test above."""
        first = index.ensure_user(kind="email", value="a@example.com")
        second = index.ensure_user(kind="email", value="b@example.com")
        assert first.id != second.id

    def test_a_phone_number_identifies_a_user_too(self, index):
        """Question 17: an email address *or* a phone number."""
        assert index.ensure_user(kind="phone", value="+43660123456").id

    def test_an_unknown_identity_kind_is_refused(self, index):
        with pytest.raises(IndexError_):
            index.ensure_user(kind="carrier-pigeon", value="x")


class TestTrips:
    def test_a_user_holds_many_trips(self, index):
        user = index.ensure_user(kind="email", value="a@example.com")
        for name in ("Europe", "Japan"):
            index.create_trip(owner_id=user.id, name=name, trip_id=new_id())
        assert {trip.name for trip in index.list_trips(owner_id=user.id)} == {"Europe", "Japan"}

    def test_a_trip_belonging_to_someone_else_is_invisible(self, index):
        mine = index.ensure_user(kind="email", value="a@example.com")
        yours = index.ensure_user(kind="email", value="b@example.com")
        trip = index.create_trip(owner_id=mine.id, name="Europe", trip_id=new_id())
        assert index.get_trip(owner_id=yours.id, trip_id=trip.id) is None

    def test_the_owner_can_see_their_own_trip(self, index):
        """The control: without it, `get_trip` returning None always would pass the test above."""
        mine = index.ensure_user(kind="email", value="a@example.com")
        trip = index.create_trip(owner_id=mine.id, name="Europe", trip_id=new_id())
        assert index.get_trip(owner_id=mine.id, trip_id=trip.id) is not None

    def test_another_users_trip_is_absent_from_the_list(self, index):
        mine = index.ensure_user(kind="email", value="a@example.com")
        yours = index.ensure_user(kind="email", value="b@example.com")
        index.create_trip(owner_id=mine.id, name="Europe", trip_id=new_id())
        assert index.list_trips(owner_id=yours.id) == []

    def test_a_trip_for_an_unknown_owner_is_refused(self, index):
        """A foreign key, so a route that fabricates an owner id cannot orphan a trip."""
        with pytest.raises(IndexError_):
            index.create_trip(owner_id="no-such-user", name="Europe", trip_id=new_id())

    def test_a_trip_id_is_not_sequential(self, index):
        """It appears in every URL, so a guessable one is a cross-tenant read waiting to happen."""
        ids = {new_id() for _ in range(50)}
        assert len(ids) == 50
        assert all(not i.isdigit() for i in ids)


class TestAssets:
    def test_declared_assets_come_back(self, index):
        user = index.ensure_user(kind="email", value="a@example.com")
        trip = index.create_trip(owner_id=user.id, name="Europe", trip_id=new_id())
        index.record_assets(trip_id=trip.id, assets=[_asset(HASH_A, "IMG_1.jpg")])
        assert [a.media_hash for a in index.trip_assets(trip_id=trip.id)] == [HASH_A]

    def test_re_declaring_an_asset_is_not_a_second_row(self, index):
        """A client retrying a negotiate is the normal case, not an error."""
        user = index.ensure_user(kind="email", value="a@example.com")
        trip = index.create_trip(owner_id=user.id, name="Europe", trip_id=new_id())
        index.record_assets(trip_id=trip.id, assets=[_asset(HASH_A, "IMG_1.jpg")])
        index.record_assets(trip_id=trip.id, assets=[_asset(HASH_A, "IMG_1.jpg")])
        assert len(index.trip_assets(trip_id=trip.id)) == 1

    def test_two_trips_may_each_claim_the_same_asset(self, index):
        """Cross-trip dedup depends on this: one photograph, two trips, one upload."""
        user = index.ensure_user(kind="email", value="a@example.com")
        first = index.create_trip(owner_id=user.id, name="Europe", trip_id=new_id())
        second = index.create_trip(owner_id=user.id, name="Japan", trip_id=new_id())
        index.record_assets(trip_id=first.id, assets=[_asset(HASH_A, "IMG_1.jpg")])
        index.record_assets(trip_id=second.id, assets=[_asset(HASH_A, "IMG_1.jpg")])
        assert len(index.trip_assets(trip_id=second.id)) == 1

    def test_asset_order_does_not_depend_on_insertion(self, index):
        user = index.ensure_user(kind="email", value="a@example.com")
        trip = index.create_trip(owner_id=user.id, name="Europe", trip_id=new_id())
        index.record_assets(trip_id=trip.id, assets=[_asset(HASH_B, "b.jpg")])
        index.record_assets(trip_id=trip.id, assets=[_asset(HASH_A, "a.jpg")])
        hashes = [a.media_hash for a in index.trip_assets(trip_id=trip.id)]
        assert hashes == sorted(hashes)


class TestPersistence:
    def test_the_index_survives_a_reopen(self, tmp_path):
        """It is a file on a disk, and a restarted container must find its trips again."""
        path = tmp_path / "index.db"
        first = SqliteIndex(path)
        user = first.ensure_user(kind="email", value="a@example.com")
        trip = first.create_trip(owner_id=user.id, name="Europe", trip_id=new_id())
        first.close()

        second = SqliteIndex(path)
        try:
            assert second.get_trip(owner_id=user.id, trip_id=trip.id) is not None
        finally:
            second.close()
