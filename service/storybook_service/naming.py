"""Hashes, filenames and object keys -- the three names an asset has, and how they relate.

An asset arrives with two identifiers and acquires a third:

- its **content hash**, which is what the pipeline keys everything by,
- its **filename**, which `overrides.toml` addresses by and which therefore has to survive
  end to end (`src/story_book/overrides.py`),
- its **object key**, which the service invents and the client never needs to see.

Each of the three has a way of going wrong that this module exists to prevent, and none of them
is hypothetical -- the filename one is a directory traversal and the stored-name one silently
loses a photograph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

# `hashlib.blake2b()` with default parameters is a 64-byte digest, hex-encoded to 128 characters
# (`src/story_book/pipeline/scan.py`). Accepting an `asset_id` prefix instead would negotiate
# against a hash the pipeline never computes, so every asset would come back needed forever and
# nothing would say why.
HASH_RE = re.compile(r"^[0-9a-f]{128}$")
HASH_HEX_LEN = 128

MAX_FILENAME_LEN = 255

ASSET_SCOPES = ("user", "content")


class NamingError(ValueError):
    """A name the service refuses, with a reason a client can act on."""


def validate_hash(value: str) -> str:
    if not HASH_RE.match(value):
        raise NamingError(
            f"hash must be {HASH_HEX_LEN} lowercase hex characters "
            f"(hashlib.blake2b default digest size); got {len(value)} characters"
        )
    return value


def validate_filename(value: str) -> str:
    """Reject anything that is not a single, plain path component.

    The service writes this name into a directory that `story-book build` then reads, so a
    client-supplied `../../etc/authorized_keys` is a write outside the trip. Windows separators
    are checked too: a name is only safe if it is one component under *both* conventions, and
    `PurePosixPath("a\\b").name` is the whole string.
    """
    if not value or value.strip() != value:
        raise NamingError("filename must be non-empty and free of leading/trailing whitespace")
    if len(value) > MAX_FILENAME_LEN:
        raise NamingError(f"filename must be at most {MAX_FILENAME_LEN} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise NamingError("filename must not contain control characters")
    if PurePosixPath(value).name != value or PureWindowsPath(value).name != value:
        raise NamingError(f"filename must be a single path component; got {value!r}")
    if value in {".", ".."}:
        raise NamingError(f"filename must name a file; got {value!r}")
    if value.startswith("."):
        # The scanner skips dotfiles, so accepting one would store bytes the build cannot see and
        # report an asset the trip does not contain.
        raise NamingError(f"filename must not start with '.'; got {value!r}")
    return value


def asset_key(
    *,
    scope: str,
    prefix: str,
    owner_id: str,
    media_hash: str,
) -> str:
    """Where an asset's bytes live.

    **This function is open question 4 and it does not settle it.** Both layouts are implemented
    and the choice is one setting, because the two foreclose opposite things:

    - `user` -- `{prefix}/u/{owner}/{aa}/{bb}/{hash}`. Dedup across *that user's* trips is still
      free, which is the case the design doc actually names ("the same photo in two trips uploads
      once"). Deleting an account is deleting one prefix, so question 3 stays answerable. Two
      users uploading the same photograph store it twice, which for family photographs is close
      to never.
    - `content` -- `{prefix}/c/{aa}/{bb}/{hash}`. Dedup is global, and deletion now needs
      reference counting that nothing in this service has.

    The default is `user`, for a reason beyond storage cost: **the service cannot verify that the
    bytes under a hash hash to it.** It never reads them -- that is the whole point of a presigned
    PUT -- so under a global layout a client could PUT chosen bytes at another user's asset's key
    and that user's next negotiate would answer `have`. Per-user keys make that self-inflicted
    instead of cross-tenant. A human still has to ratify the trade; see the tracker.
    """
    if scope not in ASSET_SCOPES:
        raise NamingError(f"asset_scope must be one of {ASSET_SCOPES}; got {scope!r}")
    validate_hash(media_hash)
    shard = f"{media_hash[:2]}/{media_hash[2:4]}"
    if scope == "user":
        if not owner_id:
            raise NamingError("the 'user' asset scope needs an owner id")
        return f"{prefix}/u/{owner_id}/{shard}/{media_hash}"
    return f"{prefix}/c/{shard}/{media_hash}"


@dataclass(frozen=True, slots=True)
class StoredName:
    media_hash: str
    filename: str
    """What the client called it."""
    stored_filename: str
    """What the source folder will call it, which is usually the same string."""

    @property
    def adjusted(self) -> bool:
        return self.filename != self.stored_filename


def assign_stored_names(assets: dict[str, str]) -> dict[str, StoredName]:
    """Map hash -> the name the source folder uses, disambiguating collisions.

    Two cameras both produce `IMG_0001.JPG`, and materialising a trip by writing each asset to
    `<source>/<filename>` would drop one of them -- silently, since a write over an existing file
    raises nothing. So a filename claimed by more than one hash gets a hash-derived suffix, and
    **every** member of the colliding group gets one: leaving the first alone would make the
    answer depend on which asset arrived first, and nothing derived from insertion order leaves
    this service any more than it leaves the database.

    The result is a pure function of the `{hash: filename}` set, so the same trip always
    materialises to the same names, and the client is told the adjusted name at negotiate time
    rather than discovering it in a report.
    """
    by_filename: dict[str, list[str]] = {}
    for media_hash, filename in assets.items():
        validate_hash(media_hash)
        validate_filename(filename)
        by_filename.setdefault(filename, []).append(media_hash)

    out: dict[str, StoredName] = {}
    for filename, hashes in by_filename.items():
        if len(hashes) == 1:
            media_hash = hashes[0]
            out[media_hash] = StoredName(media_hash, filename, filename)
            continue
        stem, dot, suffix = filename.rpartition(".")
        for media_hash in hashes:
            short = media_hash[:8]
            stored = f"{stem}~{short}{dot}{suffix}" if dot else f"{filename}~{short}"
            out[media_hash] = StoredName(media_hash, filename, stored)
    return out
