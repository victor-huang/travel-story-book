"""Names: the hash the pipeline keys by, the filename overrides address by, the key S3 holds."""

from __future__ import annotations

import hashlib

import pytest
from storybook_service.naming import (
    NamingError,
    asset_key,
    assign_stored_names,
    validate_filename,
    validate_hash,
)

REAL = hashlib.blake2b(b"a photograph").hexdigest()
OTHER = hashlib.blake2b(b"another photograph").hexdigest()


class TestValidateHash:
    def test_a_real_blake2b_default_digest_is_accepted(self):
        """The control for every rejection below: the thing the phone actually sends passes."""
        assert validate_hash(REAL) == REAL

    def test_the_digest_is_128_hex_characters(self):
        assert len(REAL) == 128

    def test_an_asset_id_prefix_is_rejected(self):
        """`asset_id` is a *prefix* of this hash, and negotiating on one matches nothing.

        Accepting it silently would report every asset as needed forever with no error anywhere.
        """
        with pytest.raises(NamingError):
            validate_hash(REAL[:12])

    def test_an_uppercase_digest_is_rejected(self):
        """`hexdigest()` is lowercase; accepting both would make two keys for one photograph."""
        with pytest.raises(NamingError):
            validate_hash(REAL.upper())

    def test_a_sha256_digest_is_rejected(self):
        with pytest.raises(NamingError):
            validate_hash(hashlib.sha256(b"a photograph").hexdigest())


class TestValidateFilename:
    def test_an_ordinary_filename_is_accepted(self):
        assert validate_filename("IMG_1815.mov") == "IMG_1815.mov"

    def test_a_posix_traversal_is_rejected(self):
        with pytest.raises(NamingError):
            validate_filename("../../etc/authorized_keys")

    def test_a_windows_traversal_is_rejected(self):
        r"""`PurePosixPath("..\\..\\x").name` is the whole string, so one check is not enough."""
        with pytest.raises(NamingError):
            validate_filename(r"..\..\evil.jpg")

    def test_an_absolute_path_is_rejected(self):
        with pytest.raises(NamingError):
            validate_filename("/etc/passwd")

    def test_a_nul_byte_is_rejected(self):
        with pytest.raises(NamingError):
            validate_filename("IMG_1815.mov\x00.txt")

    def test_a_dotfile_is_rejected(self):
        """The scanner skips dotfiles, so storing one would report an asset no build can see."""
        with pytest.raises(NamingError):
            validate_filename(".DS_Store")

    def test_an_accented_filename_is_accepted(self):
        """A real trip has these, and the font lesson applies to names as much as to titles."""
        assert validate_filename("Salzburg-Getreidegasse-Ö.heic")


class TestAssetKey:
    def test_the_user_scope_puts_the_owner_in_the_path(self):
        key = asset_key(scope="user", prefix="assets", owner_id="u123", media_hash=REAL)
        assert key == f"assets/u/u123/{REAL[:2]}/{REAL[2:4]}/{REAL}"

    def test_the_content_scope_omits_the_owner(self):
        key = asset_key(scope="content", prefix="assets", owner_id="u123", media_hash=REAL)
        assert "u123" not in key
        assert key.endswith(REAL)

    def test_the_two_scopes_differ(self):
        """The control: without it, both branches could return one string and every test pass."""
        assert asset_key(
            scope="user", prefix="assets", owner_id="u123", media_hash=REAL
        ) != asset_key(scope="content", prefix="assets", owner_id="u123", media_hash=REAL)

    def test_two_users_get_different_keys_for_the_same_photograph(self):
        """Why `user` is the default: the service cannot verify bytes hash to their key.

        Under the content scope these two collapse to one key, and whoever PUTs first decides what
        the other's trip contains.
        """
        mine = asset_key(scope="user", prefix="assets", owner_id="mine", media_hash=REAL)
        yours = asset_key(scope="user", prefix="assets", owner_id="yours", media_hash=REAL)
        assert mine != yours

    def test_the_user_scope_refuses_an_empty_owner(self):
        with pytest.raises(NamingError):
            asset_key(scope="user", prefix="assets", owner_id="", media_hash=REAL)

    def test_an_unknown_scope_is_refused(self):
        with pytest.raises(NamingError):
            asset_key(scope="per-galaxy", prefix="assets", owner_id="u", media_hash=REAL)


class TestAssignStoredNames:
    def test_a_unique_filename_is_left_alone(self):
        names = assign_stored_names({REAL: "IMG_1815.mov"})
        assert names[REAL].stored_filename == "IMG_1815.mov"
        assert names[REAL].adjusted is False

    def test_two_cameras_sharing_a_filename_both_get_disambiguated(self):
        """Not just the second one.

        Leaving the first alone would make the answer depend on which asset arrived first, and
        nothing derived from insertion order leaves this service.
        """
        names = assign_stored_names({REAL: "IMG_0001.JPG", OTHER: "IMG_0001.JPG"})
        assert names[REAL].stored_filename != names[OTHER].stored_filename
        assert names[REAL].adjusted and names[OTHER].adjusted
        assert names[REAL].stored_filename.endswith(".JPG")
        assert names[OTHER].stored_filename.endswith(".JPG")

    def test_a_collision_keeps_the_original_name_recoverable(self):
        names = assign_stored_names({REAL: "IMG_0001.JPG", OTHER: "IMG_0001.JPG"})
        assert names[REAL].filename == "IMG_0001.JPG"

    def test_the_result_does_not_depend_on_insertion_order(self):
        forward = assign_stored_names({REAL: "IMG_0001.JPG", OTHER: "IMG_0001.JPG"})
        backward = assign_stored_names({OTHER: "IMG_0001.JPG", REAL: "IMG_0001.JPG"})
        assert {h: n.stored_filename for h, n in forward.items()} == {
            h: n.stored_filename for h, n in backward.items()
        }

    def test_a_suffixless_filename_still_disambiguates(self):
        names = assign_stored_names({REAL: "scan", OTHER: "scan"})
        assert names[REAL].stored_filename != names[OTHER].stored_filename

    def test_an_invalid_filename_is_refused_here_too(self):
        """Belt and braces: the route validates, and so does the function it calls."""
        with pytest.raises(NamingError):
            assign_stored_names({REAL: "../escape.jpg"})
