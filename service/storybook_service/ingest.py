"""Ingest: create a trip, negotiate by hash, hand back presigned PUTs, prepare the folder.

The wire contract, which `NegotiateClient` (I20) is the consumer of:

    POST /trips                             {name}                  -> 201 {trip_id, ...}
    GET  /trips                                                     -> 200 {trips: [...]}
    GET  /trips/{trip_id}                                           -> 200 {trip, assets, ...}
    POST /trips/{trip_id}/assets:negotiate  {assets: [...]}         -> 200 {needed, have, ...}
    PUT  <put_url>                          the bytes               -> S3, not this service
    POST /trips/{trip_id}/source:prepare                            -> 200 {materialised, config}

Three properties are structural rather than tested into place:

- **Never a zip, never a proxy.** The upload granularity is one asset and the bytes go straight to
  the store. There is no route here that accepts media.
- **The unit of work stays the trip.** Chunking the transfer per asset must not divide the work; a
  trip is one `story.db`, one build, one `trip.json`. T58 shipped the opposite mistake -- a package
  split per day, and three one-day answers came back.
- **A user only ever sees their own trips**, because `Index.get_trip` takes `owner_id` and there is
  no way to ask it anything else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from storybook_service.index import Index, Trip, TripAsset
from storybook_service.index_sqlite import new_id
from storybook_service.naming import (
    HASH_HEX_LEN,
    NamingError,
    asset_key,
    assign_stored_names,
    validate_filename,
    validate_hash,
)
from storybook_service.objectstore import ObjectStore, ObjectStoreError
from storybook_service.principal import Principal, resolve_principal
from storybook_service.settings import Settings
from storybook_service.source import (
    ScaffoldError,
    materialise_source,
    scaffold_trip,
    trip_paths,
)

router = APIRouter()

MAX_ASSETS_PER_NEGOTIATE = 2000
"""A real trip is a few hundred assets; 2000 is headroom, not a target.

The bound exists because each entry costs a HEAD and a signature, so an unbounded list is a way to
make one request expensive. A client with more assets negotiates in batches -- which changes
nothing about the unit of work, since negotiation is not the build.
"""


class AssetDeclaration(BaseModel):
    hash: str = Field(description=f"blake2b-512 of the file's bytes, {HASH_HEX_LEN} lowercase hex")
    filename: str = Field(description="the original filename, preserved end to end")
    size: int = Field(ge=0, description="the file's length in bytes, as the client measured it")

    @field_validator("hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        try:
            return validate_hash(value)
        except NamingError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        try:
            return validate_filename(value)
        except NamingError as exc:
            raise ValueError(str(exc)) from exc


class NegotiateRequest(BaseModel):
    assets: list[AssetDeclaration]

    @field_validator("assets")
    @classmethod
    def _bounded_and_unique(cls, value: list[AssetDeclaration]) -> list[AssetDeclaration]:
        if not value:
            raise ValueError("negotiate at least one asset")
        if len(value) > MAX_ASSETS_PER_NEGOTIATE:
            raise ValueError(f"negotiate at most {MAX_ASSETS_PER_NEGOTIATE} assets per request")
        hashes = {asset.hash for asset in value}
        if len(hashes) != len(value):
            # Two declarations of one hash with different sizes have no correct answer, and the
            # same hash twice with the same size is the client asking a question twice.
            raise ValueError("each hash may appear at most once in a negotiate request")
        return value


class CreateTripRequest(BaseModel):
    name: str = Field(default="", max_length=200)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _index(request: Request) -> Index:
    return request.app.state.index


def _store(request: Request) -> ObjectStore:
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "no object store is configured. Set STORY_SERVICE_S3_BUCKET (and "
                "STORY_SERVICE_S3_ENDPOINT_URL for a local MinIO or moto)."
            ),
        )
    return store


PrincipalDep = Annotated[Principal, Depends(resolve_principal)]
SettingsDep = Annotated[Settings, Depends(_settings)]
IndexDep = Annotated[Index, Depends(_index)]
StoreDep = Annotated[ObjectStore, Depends(_store)]


def _trip_json(trip: Trip) -> dict[str, Any]:
    return {
        "trip_id": trip.id,
        "name": trip.name,
        "created_at": trip.created_at.isoformat(),
    }


def _require_trip(index: Index, principal: Principal, trip_id: str) -> Trip:
    trip = index.get_trip(owner_id=principal.user_id, trip_id=trip_id)
    if trip is None:
        # 404 for "not yours" as well as "not there". Distinguishing them would let a caller
        # enumerate which trip ids exist, which is a cross-tenant read in slow motion.
        raise HTTPException(status_code=404, detail=f"no trip {trip_id!r}")
    return trip


@router.post("/trips", status_code=201)
def create_trip(
    body: CreateTripRequest,
    principal: PrincipalDep,
    index: IndexDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Create a trip and its working directory.

    **No config is scaffolded here.** `story-book init` profiles the source folder, and at this
    moment the folder is empty -- it would write a config in which nothing was measured and warn
    about it into a log nobody reads. Scaffolding happens in `source:prepare`, once the media is
    actually there.
    """
    trip = index.create_trip(
        owner_id=principal.user_id,
        name=body.name or "Untitled trip",
        trip_id=new_id(),
    )
    paths = trip_paths(settings, trip.id)
    paths.source.mkdir(parents=True, exist_ok=True)
    return {
        **_trip_json(trip),
        "source_config": "not scaffolded yet: story-book init profiles the media, so it runs in "
        "source:prepare once the assets have landed",
    }


@router.get("/trips")
def list_trips(principal: PrincipalDep, index: IndexDep) -> dict[str, Any]:
    return {"trips": [_trip_json(trip) for trip in index.list_trips(owner_id=principal.user_id)]}


@router.get("/trips/{trip_id}")
def get_trip(trip_id: str, principal: PrincipalDep, index: IndexDep) -> dict[str, Any]:
    trip = _require_trip(index, principal, trip_id)
    assets = index.trip_assets(trip_id=trip.id)
    return {
        **_trip_json(trip),
        "assets": [
            {
                "hash": asset.media_hash,
                "filename": asset.filename,
                "stored_filename": asset.stored_filename,
                "size": asset.size,
            }
            for asset in assets
        ],
        "asset_scope": "declared",
        "asset_scope_detail": (
            "every asset this trip has declared, whether or not its bytes have been uploaded. "
            "source:prepare reports which are actually present."
        ),
    }


@router.post("/trips/{trip_id}/assets:negotiate")
def negotiate_assets(
    trip_id: str,
    body: NegotiateRequest,
    principal: PrincipalDep,
    index: IndexDep,
    store: StoreDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Return only what the store lacks, with a presigned PUT for each.

    Two things this endpoint refuses to claim:

    - **`have` means the store holds an object of the declared length at that key.** It does not
      mean the bytes hash to it -- the service never reads them, which is what "never proxy the
      media" costs. A stored length that disagrees with the declaration is therefore treated as
      *not* present, so the asset is re-uploaded rather than believed.
    - The URLs are grants, not confirmations. Nothing here records an upload as done; the next
      negotiate asks the store again.
    """
    trip = _require_trip(index, principal, trip_id)

    declared = {asset.hash: asset.filename for asset in body.assets}
    sizes = {asset.hash: asset.size for asset in body.assets}
    # Names are assigned over the trip's whole asset set, not just this batch: a second negotiate
    # adding `IMG_0001.JPG` from another camera has to rename *both*, and a per-batch answer could
    # not see the first one. So the earlier asset's row is rewritten too -- the stored name is a
    # function of the set, and `materialise_source` reconciles the folder to it.
    known = {asset.media_hash: asset for asset in index.trip_assets(trip_id=trip.id)}
    filenames = {media_hash: asset.filename for media_hash, asset in known.items()} | declared
    stored_names = assign_stored_names(filenames)

    now = datetime.now(UTC)
    index.record_assets(
        trip_id=trip.id,
        assets=[
            TripAsset(
                media_hash=media_hash,
                filename=filename,
                stored_filename=stored_names[media_hash].stored_filename,
                size=sizes[media_hash] if media_hash in sizes else known[media_hash].size,
                declared_at=now if media_hash in declared else known[media_hash].declared_at,
            )
            for media_hash, filename in filenames.items()
        ],
    )

    needed: list[dict[str, Any]] = []
    have: list[dict[str, Any]] = []
    try:
        for media_hash, filename in declared.items():
            key = asset_key(
                scope=settings.asset_scope,
                prefix=settings.s3_asset_prefix,
                owner_id=principal.user_id,
                media_hash=media_hash,
            )
            stored = stored_names[media_hash]
            info = store.head(key)
            if info is not None and info.size == sizes[media_hash]:
                have.append({"hash": media_hash, "size": info.size})
                continue
            upload = store.presign_put(key, size=sizes[media_hash])
            needed.append(
                {
                    "hash": media_hash,
                    "filename": filename,
                    "stored_filename": stored.stored_filename,
                    "filename_adjusted": stored.adjusted,
                    "size": sizes[media_hash],
                    "method": upload.method,
                    "put_url": upload.url,
                    "headers": upload.headers,
                    "expires_at": upload.expires_at.isoformat(),
                    # Present and false when the store held an object of the wrong length: the
                    # client is re-uploading something the service already had *something* for,
                    # and saying so is cheaper than a support conversation.
                    "replaces_mismatched_object": info is not None,
                }
            )
    except ObjectStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "trip_id": trip.id,
        "needed": needed,
        "have": have,
        "upload": {
            "transport": "presigned PUT straight to object storage",
            "granularity": "one asset per request",
            "multipart": False,
            "multipart_detail": (
                "a single presigned PUT carries up to 5 GB and a 1080p export is tens of MB, so "
                "multipart is not implemented; it would buy resume *within* one file, not "
                "capability"
            ),
            "presence_check": "the store holds an object of the declared length at that key",
            "presence_not_verified": (
                "the service never reads the bytes, so it does not confirm they hash to the key"
            ),
        },
    }


@router.post("/trips/{trip_id}/source:prepare")
def prepare_source(
    trip_id: str,
    principal: PrincipalDep,
    index: IndexDep,
    store: StoreDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Fetch the uploaded media onto the filesystem and scaffold a config over it.

    **An addition to the endpoint list in `ios_backend_service.md`**, which goes straight from the
    presigned PUT to `POST /trips/{id}/build`. Two reasons it is a route of its own rather than the
    first thing a build does: the scaffolded config is measured from the media, so a client that
    wants to inspect or edit it needs a moment when it exists and no build is running; and it is
    idempotent, so S03 may perfectly well call it as build step zero. If S03 folds it in, this
    route becomes a read-only alias and nothing else changes.
    """
    trip = _require_trip(index, principal, trip_id)
    assets = index.trip_assets(trip_id=trip.id)
    try:
        materialised = materialise_source(
            settings=settings,
            store=store,
            owner_id=principal.user_id,
            trip_id=trip.id,
            assets=assets,
        )
    except ObjectStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    paths = trip_paths(settings, trip.id)
    body: dict[str, Any] = {
        "trip_id": trip.id,
        "source": str(paths.source),
        "out": str(paths.out),
        "declared": len(assets),
        "fetched": len(materialised.fetched),
        "already_present": len(materialised.already_present),
        "removed": materialised.removed,
        "missing": materialised.missing,
        "complete": materialised.complete,
    }

    if not materialised.complete:
        # Not scaffolded, and the reason is the interesting half. `story-book init` profiles the
        # folder; profiling a folder that is four photographs short writes measurements of the
        # wrong trip into a file that will never be re-measured, because init refuses to
        # overwrite it.
        body["scaffolded"] = False
        body["scaffold_detail"] = (
            f"{len(materialised.missing)} declared asset(s) have not been uploaded. "
            "story-book init measures the source folder, and a config measured from a partial "
            "trip is not rewritten later -- so scaffolding waits."
        )
        return body

    try:
        scaffold = scaffold_trip(settings=settings, trip_id=trip.id, trip_name=trip.name)
    except ScaffoldError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    body["scaffolded"] = True
    body["config"] = str(scaffold.config)
    body["overrides"] = str(scaffold.overrides)
    body["config_created_now"] = scaffold.created
    body["home_configured"] = False
    body["home_detail"] = (
        "no [home] block: the service has no home coordinate for this user, so nothing is "
        "excluded from exports by proximity to where they live. The iOS client filters before "
        "upload (I14), which is the only placement that keeps those photographs off the server."
    )
    return body
