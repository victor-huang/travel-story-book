"""Delivery: the report bundle the app loads offline, and the media it points at.

**S05, and the last piece that closes the loop** (D14): export, upload, build and now this --
without it a succeeded job has nowhere to go. The design question, settled against the actual
code rather than assumed:

`ios_client_app.md`/I24 describes the app loading the report exactly as `story-book build` writes
it -- `report/`, `thumbs/`, `previews/`, `.cache/video/` as four sibling roots, with plain
relative `../thumbs/...` references, handed to `WKWebView.loadFileURL`. Per-file signed URLs would
break every one of those relative links (D24), so under that design the only sound shape is one
bundle carrying all four roots.

But I25 (`AssetSchemeHandler.swift`), which landed the same day and is the one that actually runs,
renders the report with `media_rel="storyasset://"` instead (D4) -- every image reference becomes
a scheme URL the app resolves itself: the phone's own `PHAsset` first, a server preview second,
a placeholder last. Under *that* design the bundle needs no media at all, because there is no
local reference to a media file left in the HTML to break.

**This module builds the I25 shape**, because it is what `ReportLoader` actually registers
(`AssetSchemeHandler? = .init()` is not optional) and because it is strictly less to ship: the
bundle is html/css/js, typically tens of kilobytes, not the trip's thumbnails and previews. The
`story-book build` a worker runs is untouched -- it still writes the plain relative-path report
next to `trip.json`, exactly as the laptop workflow always has (`cli.py:332`) -- this module
re-renders *from `trip.json`*, which is a fraction of a second's work, into a second copy meant
for the wire.

**A gap this surfaced, left for whoever next touches `AssetSchemeHandler.swift`:** a video's
poster frame is stored at `.cache/video/<hash>_poster.jpg` (`pipeline/video.py:419`), which
`_derived_images` (`pipeline/timeline.py:366-368`) uses as *both* the asset's thumbnail and its
preview. Rendered with `media_rel="storyasset://"` that becomes
`storyasset://.cache/video/<hash>_poster.jpg` -- host `.cache`, which
`AssetRequestParsing.parse` does not recognise (it matches only `thumbs` and `previews`), so
today's client fails every video poster with `unrecognizedRequest`. This module does not paper
over it: it reports honestly which assets have a thumbnail/preview at all
(`ReportBundleResult.media_summary`), and `GET /trips/{id}/media/{relpath}` serves *any* relpath
`trip.json` names, poster included, so the fix is a client-side parsing change (a third host case,
or the report emitting `.cache/video/...` under the `previews` host) rather than anything the
service can do without touching `ios/**`.

**Ownership is by trip.json's own claim, not by a client's request.** `GET .../media/{relpath}`
only ever serves a path this trip's own `trip.json` names as some asset's `thumbnail` or
`preview` -- never an arbitrary path under `--out`. The same discipline `materialise_source`
applies to what a folder may contain, applied to what a URL may read.

**Immutable once a job succeeds.** The bundle and the delivery keys are namespaced by `job_id`, so
a re-run creates a new job and a new zip rather than mutating one already handed out (mirrors D5's
reasoning for posters and reels). Re-requesting is cheap and idempotent: `presign_get` is called
again, `put_file` is skipped once `head` agrees on size, and the zip is rendered once and never
rewritten.
"""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from story_book.export.report import MediaPrefix, render_report
from storybook_service.index import Index, Job
from storybook_service.objectstore import ObjectStore, ObjectStoreError
from storybook_service.principal import Principal, resolve_principal
from storybook_service.reel_jobs import reel_output_paths
from storybook_service.settings import Settings
from storybook_service.source import TripPaths, trip_paths

router = APIRouter()

ASSET_SCHEME = "storyasset://"
"""What every media reference in the delivered report is prefixed with (D4, I25)."""

_LOCAL_REF_RE = re.compile(r'(?:src|href)="([^"]+)"')


class DeliveryError(Exception):
    """The bundle could not be assembled, with a reason a caller can act on."""


def _is_local(reference: str) -> bool:
    """Same rule as `ReportBundle.isLocal` in `ReportWebView.swift`: a scheme or `//` is not us."""
    if reference.startswith("#") or reference.startswith("//"):
        return False
    return "://" not in reference


def local_references(html: str) -> list[str]:
    return [ref for ref in _LOCAL_REF_RE.findall(html) if _is_local(ref.split("#", 1)[0])]


def unresolved_local_references(report_dir: Path) -> list[str]:
    """Every local (non-`storyasset://`) reference in the rendered report that does not resolve.

    Mirrors `ReportBundle.unresolvedReferences()` on the client, asked from this side of the wire
    before the bundle ships rather than after it is downloaded. With `media_rel="storyasset://"`
    the only local references left are the stylesheet and vendored Leaflet -- everything else is
    a scheme URL and is deliberately excluded, since those resolve dynamically, not from this zip.
    """
    problems: list[str] = []
    pages = [report_dir / "index.html"]
    days_dir = report_dir / "days"
    if days_dir.is_dir():
        pages += sorted(days_dir.glob("*.html"))
    root = report_dir.resolve()
    for page in pages:
        html = page.read_text()
        for ref in local_references(html):
            path = ref.split("#", 1)[0]
            target = (page.parent / path).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                problems.append(f"{page.name} -> {ref} (outside the bundle)")
                continue
            if not target.exists():
                problems.append(f"{page.name} -> {ref} (missing)")
    return problems


@dataclass(frozen=True, slots=True)
class ReportBundleResult:
    zip_path: Path
    media_summary: dict[str, int]


def _media_summary(doc: dict) -> dict[str, int]:
    assets = doc.get("assets", {})
    return {
        "total": len(assets),
        "with_thumbnail": sum(1 for a in assets.values() if a.get("thumbnail")),
        "with_preview": sum(1 for a in assets.values() if a.get("preview")),
    }


def build_app_report_bundle(*, paths: TripPaths, job_id: str) -> ReportBundleResult:
    """Render the `storyasset://` report from `trip.json` and zip it, once per job.

    Idempotent and cheap on every call after the first: if the zip already exists it is returned
    unchanged, because nothing about a succeeded job's `trip.json` can change under it.
    """
    trip_json_path = paths.out / "trip.json"
    if not trip_json_path.is_file():
        raise DeliveryError(f"no {trip_json_path}; this job has not produced a trip yet")
    doc = json.loads(trip_json_path.read_text())
    summary = _media_summary(doc)

    delivery_dir = paths.trip_dir / "delivery" / job_id
    zip_path = delivery_dir / "report.zip"
    if zip_path.is_file():
        return ReportBundleResult(zip_path=zip_path, media_summary=summary)

    render_dir = delivery_dir / "render"
    if render_dir.exists():
        shutil.rmtree(render_dir)
    render_dir.mkdir(parents=True)
    try:
        render_report(doc, render_dir, media_prefix=MediaPrefix.absolute(ASSET_SCHEME))
        report_dir = render_dir / "report"

        unresolved = unresolved_local_references(report_dir)
        if unresolved:
            # Never ship a bundle that renders unstyled or with a blank map -- that is silently
            # wrong rather than visibly absent, which is worse. Caught here, before delivery,
            # rather than by a reader wondering why the page looks broken.
            raise DeliveryError(
                f"the rendered report has {len(unresolved)} unresolved local reference(s), so it "
                f"was not shipped: {unresolved}"
            )

        delivery_dir.mkdir(parents=True, exist_ok=True)
        partial = zip_path.with_name(zip_path.name + ".partial")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(report_dir.rglob("*")):
                if file.is_file():
                    archive.write(file, arcname=str(Path("report") / file.relative_to(report_dir)))
        partial.replace(zip_path)
    finally:
        shutil.rmtree(render_dir, ignore_errors=True)
    return ReportBundleResult(zip_path=zip_path, media_summary=summary)


def asset_relpaths(doc: dict) -> set[str]:
    """Every thumbnail/preview path this trip's own `trip.json` claims -- the whitelist.

    A client asking for anything else gets a 404 regardless of whether a file happens to exist at
    that path under `--out`: this is the one boundary that decides what `GET .../media/{relpath}`
    may read, and it is derived from data the pipeline published, not from the request.
    """
    paths: set[str] = set()
    for asset in doc.get("assets", {}).values():
        for key in ("thumbnail", "preview"):
            value = asset.get(key)
            if value:
                paths.add(value)
    return paths


def delivery_asset_key(*, prefix: str, owner_id: str, trip_id: str, relpath: str) -> str:
    """Where a derived artifact lives once uploaded for delivery.

    Namespaced apart from `naming.asset_key` on purpose: these are pipeline output, not the
    traveller's own bytes, and deleting a trip's delivery cache must never touch the originals.
    """
    return f"{prefix}/u/{owner_id}/{trip_id}/{relpath}"


def ensure_uploaded(store: ObjectStore, key: str, local_path: Path) -> None:
    """Upload once. A derived artifact under a succeeded job's `--out` never changes size."""
    size = local_path.stat().st_size
    info = store.head(key)
    if info is not None and info.size == size:
        return
    store.put_file(key, local_path)


# --- routes -----------------------------------------------------------------------------------


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
                "STORY_SERVICE_S3_ENDPOINT_URL for a local MinIO or moto), or set "
                "STORY_SERVICE_OBJECT_STORE_BACKEND=local and STORY_SERVICE_PUBLIC_BASE_URL "
                "for a same-Wi-Fi test with no bucket at all."
            ),
        )
    return store


PrincipalDep = Annotated[Principal, Depends(resolve_principal)]
SettingsDep = Annotated[Settings, Depends(_settings)]
IndexDep = Annotated[Index, Depends(_index)]
StoreDep = Annotated[ObjectStore, Depends(_store)]


def _capability_json(job: Job) -> dict[str, Any]:
    if not job.capability:
        return {}
    try:
        return json.loads(job.capability)
    except json.JSONDecodeError:  # pragma: no cover - written by this codebase only
        return {}


def _require_job(index: Index, principal: Principal, job_id: str) -> Job:
    job = index.get_job(owner_id=principal.user_id, job_id=job_id)
    if job is None:
        # Identical to "no such job" -- see jobs.py for why this must not distinguish the two.
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    return job


@router.get("/jobs/{job_id}/report")
def get_report_bundle(
    job_id: str,
    principal: PrincipalDep,
    index: IndexDep,
    settings: SettingsDep,
    store: StoreDep,
) -> dict[str, Any]:
    """The report bundle for one succeeded job: html/css/vendored Leaflet, no media inside it.

    409 while the job is still queued or running -- there is nothing to deliver yet, and the
    right response is "keep polling `GET /jobs/{job_id}`", not a 404 that reads as "never will
    be". A failed job is not silently offered either: its own `error` is what a caller should
    read, not a stale report from an earlier attempt.
    """
    job = _require_job(index, principal, job_id)
    if job.state != "succeeded":
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id} is {job.state!r}, not succeeded; there is no report bundle yet. "
                f"Poll GET /jobs/{job_id} until state is succeeded."
            ),
        )
    paths = trip_paths(settings, job.trip_id)
    try:
        built = build_app_report_bundle(paths=paths, job_id=job_id)
    except DeliveryError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    key = delivery_asset_key(
        prefix=settings.s3_delivery_prefix,
        owner_id=job.owner_id,
        trip_id=job.trip_id,
        relpath=f"jobs/{job_id}/report.zip",
    )
    try:
        ensure_uploaded(store, key, built.zip_path)
        download = store.presign_get(
            key, filename="report.zip", ttl_s=settings.delivery_presign_ttl_s
        )
    except ObjectStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    capability = _capability_json(job)
    return {
        "job_id": job.id,
        "trip_id": job.trip_id,
        "state": job.state,
        # Never papered over: a degraded build's bundle is complete and correct for what the
        # pipeline computed, and the client is told exactly which stages narrowed the result --
        # the artifact must not look more complete than it is.
        "degraded": capability.get("degraded"),
        "unavailable_stages": capability.get("unavailable", []),
        "bundle": {
            "format": "zip",
            "contains": (
                "report/ only: index.html, days/*.html, style.css, vendor/leaflet.{js,css}. No "
                "thumbs/, previews/ or .cache/ -- every media reference is a storyasset:// URL, "
                "resolved locally or through GET /trips/{trip_id}/media/{relpath}."
            ),
            "media_rel": ASSET_SCHEME,
            "download_url": download.url,
            "expires_at": download.expires_at.isoformat(),
            "size_bytes": built.zip_path.stat().st_size,
        },
        "media_summary": built.media_summary,
    }


@router.get("/trips/{trip_id}/media/{relpath:path}")
def get_media(
    trip_id: str,
    relpath: str,
    principal: PrincipalDep,
    index: IndexDep,
    settings: SettingsDep,
    store: StoreDep,
) -> RedirectResponse:
    """One derived image -- a thumbnail, a preview, or a video's poster frame -- by its own path.

    The path is not client-chosen: it must be one `trip.json` already names as some asset's
    `thumbnail` or `preview`, checked against the trip's own file on every request rather than
    cached, because a rebuild can change which files exist. Backs `AssetSchemeHandler`'s tier 2
    (I25) -- whichever way a future client parses the scheme URL, the mapping from an asset to a
    relative path is exactly what `trip.json` already computed, and this route serves it by that
    path rather than by re-deriving a hash-to-kind rule the client and service could drift on.
    """
    trip = index.get_trip(owner_id=principal.user_id, trip_id=trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail=f"no trip {trip_id!r}")
    paths = trip_paths(settings, trip.id)
    trip_json_path = paths.out / "trip.json"
    if not trip_json_path.is_file():
        raise HTTPException(status_code=404, detail="this trip has no build yet")
    doc = json.loads(trip_json_path.read_text())
    if relpath not in asset_relpaths(doc):
        raise HTTPException(
            status_code=404,
            detail=f"{relpath!r} is not a media reference in this trip's current report",
        )
    local_path = paths.out / relpath
    if not local_path.is_file():
        # `trip.json` claims it; the pipeline degrades rather than fabricating a broken link, and
        # neither should this route -- a 404 says the truth rather than presigning a URL for
        # bytes that are not there.
        raise HTTPException(
            status_code=404, detail=f"{relpath!r} is referenced but the file is not on disk"
        )

    key = delivery_asset_key(
        prefix=settings.s3_delivery_prefix, owner_id=trip.owner_id, trip_id=trip.id, relpath=relpath
    )
    try:
        ensure_uploaded(store, key, local_path)
        download = store.presign_get(key, ttl_s=settings.delivery_presign_ttl_s)
    except ObjectStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RedirectResponse(url=download.url, status_code=302)


@router.get("/jobs/{job_id}/reel")
def get_reel_download(
    job_id: str,
    principal: PrincipalDep,
    index: IndexDep,
    settings: SettingsDep,
    store: StoreDep,
) -> dict[str, Any]:
    """S07: the finished video and the honest record of what rendered it, for one succeeded reel
    job. Same shape as `GET /jobs/{job_id}/report` (S05) -- a signed `GET`, never a public-read
    bucket, scoped by the same owner-checked join every other route here uses.

    **Immutable per D5, so there is nothing to invalidate.** The key is namespaced by `job_id`;
    a re-cut is a different job, a different key, a different signed URL -- never a rewrite of
    this one. No ETag, no revalidation: I33's cache may key on `job_id` alone and never re-check.
    """
    job = _require_job(index, principal, job_id)
    if job.kind != "reel":
        raise HTTPException(
            status_code=404, detail=f"job {job_id} is a {job.kind!r} job, not a reel"
        )
    if job.state != "succeeded":
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id} is {job.state!r}, not succeeded; there is no reel yet. "
                f"Poll GET /jobs/{job_id} until state is succeeded."
            ),
        )

    try:
        options = json.loads(job.options) if job.options else {}
    except json.JSONDecodeError:  # pragma: no cover - written by this codebase only
        options = {}
    paths = trip_paths(settings, job.trip_id)
    video_path, manifest_path = reel_output_paths(paths, options)
    if not video_path.is_file() or not manifest_path.is_file():
        # The worker itself refuses to call a reel job "succeeded" without both files (P06's
        # lesson, applied at write time); this is the same check applied again at read time,
        # because a job row and a filesystem can still disagree if something moved between them.
        missing = video_path if not video_path.is_file() else manifest_path
        raise HTTPException(
            status_code=500,
            detail=f"job {job_id} is succeeded but {missing} is missing on disk",
        )
    manifest = json.loads(manifest_path.read_text())

    key = delivery_asset_key(
        prefix=settings.s3_delivery_prefix,
        owner_id=job.owner_id,
        trip_id=job.trip_id,
        relpath=f"jobs/{job_id}/{video_path.name}",
    )
    try:
        ensure_uploaded(store, key, video_path)
        download = store.presign_get(
            key, filename=video_path.name, ttl_s=settings.delivery_presign_ttl_s
        )
    except ObjectStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "job_id": job.id,
        "trip_id": job.trip_id,
        "state": job.state,
        "video": {
            "format": "mp4",
            "download_url": download.url,
            "expires_at": download.expires_at.isoformat(),
            "size_bytes": video_path.stat().st_size,
        },
        "immutable": True,
        "immutable_detail": (
            "a re-cut is a new job with a new job_id (D5), never a mutation of this one; no ETag "
            "or revalidation is needed on the client side."
        ),
        # reel.json is "the honest record of what a render actually did" (the tracker's frozen
        # list); shipped whole rather than as a second signed URL, since I30's own acceptance
        # criterion is that every option it sent is reflected in it.
        "reel_json": manifest,
    }
