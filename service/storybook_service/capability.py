"""What this deployment can actually do, measured rather than assumed.

Two rules from the project's history shape this module.

*An artifact never overstates its contents.* A hosted build is only as good as the binaries and
optional extras present in the image, and the pipeline is written to **degrade rather than abort**
when one is missing -- so a `clip`-less image still returns a `trip.json`, with no CLIP clustering
in it. If the service does not say which dependencies it has, a caller cannot tell a complete build
from a degraded one, and every later artifact inherits that ambiguity.

*Read with the pipeline's own code.* Every probe below delegates to the predicate the corresponding
stage calls in `available()`, and quotes the stage's own `description`. Re-deriving "is ffmpeg
usable" here would be a second answer to a question the pipeline already answers, and the two would
drift.
"""

from __future__ import annotations

import importlib.util
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

from story_book.exif import exiftool_available
from story_book.pipeline import quality as quality_stage
from story_book.pipeline.embeddings import EmbeddingStage, clip_importable
from story_book.pipeline.geocode import GeocodeStage
from story_book.pipeline.geocode import available as geocode_available
from story_book.pipeline.metadata import MetadataStage
from story_book.pipeline.quality import QualityStage
from story_book.pipeline.video import VideoStage, ffmpeg_available
from storybook_service.settings import Settings


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    # Empty when a probe succeeded and had nothing further to report; never a fabricated reason.
    detail: str
    # False means "absent degrades the pipeline"; readiness does not depend on it.
    required: bool
    # What stops working when `ok` is false, in the words of the stage that stops working.
    affects: str


@dataclass(frozen=True)
class Report:
    ready: bool
    checks: tuple[Check, ...]
    measured_at: datetime
    """When the probe ran -- not when it was read.

    The probe runs once at startup: binaries and installed packages do not appear inside a running
    container, and spawning a subprocess per readiness poll would be a cost with no information in
    it. Publishing the timestamp is the difference between reporting a measurement and implying a
    fresh one.
    """


def _cli_version(settings: Settings) -> Check:
    """Runs `story-book --version` the way the service will run `story-book build`.

    Deliberately a subprocess and deliberately resolved by name. `import story_book` would prove
    the library is importable by *this* interpreter, which is a different claim: S03 runs the CLI,
    so what has to be true is that the executable is on PATH in this image.
    """
    argv = [settings.story_book_bin, "--version"]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=settings.probe_timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(
            name="story_book_cli",
            ok=False,
            detail=f"{' '.join(argv)}: {exc}",
            required=True,
            affects="every build, report, package and reel job",
        )
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        return Check(
            name="story_book_cli",
            ok=False,
            detail=f"exit {completed.returncode}: {output}",
            required=True,
            affects="every build, report, package and reel job",
        )
    return Check(
        name="story_book_cli",
        ok=True,
        detail=output,
        required=True,
        affects="every build, report, package and reel job",
    )


def _spec_installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def probe(settings: Settings | None = None) -> Report:
    settings = settings or Settings.from_env()

    exiftool_ok = exiftool_available()
    ffmpeg_ok, ffmpeg_detail = ffmpeg_available()
    geo_ok, geo_detail = geocode_available()
    clip_ok, clip_detail = clip_importable()
    whisper_ok = _spec_installed("faster_whisper")

    checks = (
        _cli_version(settings),
        Check(
            name="exiftool",
            ok=exiftool_ok,
            detail="" if exiftool_ok else "exiftool binary not found on PATH",
            required=True,
            affects=MetadataStage.description,
        ),
        Check(
            name="ffmpeg",
            ok=ffmpeg_ok,
            detail=ffmpeg_detail,
            required=True,
            affects=VideoStage.description,
        ),
        Check(
            name="reverse_geocoder",
            ok=geo_ok,
            detail=geo_detail,
            required=False,
            affects=GeocodeStage.description,
        ),
        Check(
            name="opencv",
            ok=quality_stage.cv2 is not None,
            detail=""
            if quality_stage.cv2 is not None
            else "opencv-python-headless is not installed (install the 'images' extra)",
            required=False,
            affects=QualityStage.description,
        ),
        Check(
            name="clip",
            ok=clip_ok,
            detail=clip_detail,
            required=False,
            affects=EmbeddingStage.description,
        ),
        Check(
            name="faster_whisper",
            ok=whisper_ok,
            detail="" if whisper_ok else "faster-whisper is not installed (the 'video' extra)",
            required=False,
            # The video stage catches a transcription failure and continues, so this degrades the
            # clip's transcript rather than the stage.
            affects="speech transcripts for video clips",
        ),
    )
    return Report(
        ready=all(check.ok for check in checks if check.required),
        checks=checks,
        measured_at=datetime.now(UTC),
    )
