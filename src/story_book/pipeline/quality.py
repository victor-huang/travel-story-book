"""Module 8: quality scoring.

Two stages, both writing to the `score` table:

* `QualityStage` -- cheap, deterministic OpenCV signals computed per photo: sharpness,
  exposure, contrast, and a face-presence signal. Combined into an explicit, documented
  weighted sum (`overall`) using `config.quality.weights`.
* `ContentClassStage` -- CLIP zero-shot classification of what the photo actually shows
  (screenshot / receipt / document / food / landscape / group photo / other), so the
  selection stage can reject screenshots and receipts before they ever reach a human.

Phase 1 only. Smile detection, eyes-open detection, "people centered" framing, and any
learned aesthetic/composition model are explicitly deferred to Phase 2 -- see Module 8 of
`dev_plan/mvp_process_from_picture_to_stories.md`. Do not add them here.

`ContentClassStage` depends on the CLIP runner that T14 owns
(`story_book.pipeline.embeddings.ClipRunner` / `load_clip`). That module may not exist yet
in a given checkout, so the import is guarded; `ContentClassStage.available()` reports the
stage as unavailable (never crashes the run) until it lands.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path
from typing import Any

from story_book.config import Config, QualityWeights
from story_book.db.connection import iter_media
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import BatchStage, Executor, PerItemStage, SkipItem, StageContext

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - exercised via QualityStage.available()
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

try:
    from story_book.pipeline.embeddings import ClipRunner, clip_importable, load_clip
except ImportError:  # pragma: no cover - T14 may not have landed yet in this checkout
    ClipRunner = None  # type: ignore[assignment,misc]
    clip_importable = None  # type: ignore[assignment]
    load_clip = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# --- content classification ------------------------------------------------------------

CONTENT_CLASSES: tuple[str, ...] = (
    "screenshot",
    "receipt",
    "document",
    "food",
    "landscape",
    "group_photo",
    "other",
)
"""Zero-shot label set for `ContentClassStage`.

Must be a superset of `config.quality.reject_content_classes` (screenshot/receipt/document
by default) plus at least food/landscape/group_photo/other -- see the T13 tracker entry.
"""

# --- sharpness --------------------------------------------------------------------------

# Laplacian variance grows with detail *and* with pixel count for an equally sharp scene
# (more pixels means more independently-sampled edges), so we divide by pixel count before
# comparing images of different resolutions -- otherwise a bigger blurry photo could
# outscore a smaller sharp one. `SHARPNESS_SCALE` is a display-range calibration constant
# only: it feeds a saturating `1 - exp(-x)` curve, so it changes how spread out scores look
# but never changes the *ordering* of two images -- sharper always scores higher.
SHARPNESS_SCALE = 200.0

# --- exposure -----------------------------------------------------------------------------

# A small tolerance band around absolute black/white (rather than exactly 0/255) so that
# sensor and JPEG compression noise near true clipping still counts as clipped.
EXPOSURE_SHADOW_CUTOFF = 10
EXPOSURE_HIGHLIGHT_CUTOFF = 245

# --- contrast -----------------------------------------------------------------------------

# The maximum possible standard deviation of 8-bit pixel values, attained by a bimodal
# image split evenly between 0 and 255. Used to normalize RMS contrast into [0, 1].
CONTRAST_MAX_STD = 127.5

# --- faces --------------------------------------------------------------------------------

# Standard Haar cascade detection parameters -- not business thresholds, just the knobs
# OpenCV's detector itself exposes.
FACE_SCALE_FACTOR = 1.1
FACE_MIN_NEIGHBORS = 5
FACE_MIN_SIZE_PX = 24

# A face filling this fraction of the frame or more saturates the face-presence score at 1.0.
FACE_FRAC_SATURATION = 0.15

# Absence of a face is not evidence of a bad photo -- many travel highlights are landscapes
# or food with nobody in frame -- so a faceless photo gets a neutral score rather than 0.
FACE_NEUTRAL_SCORE = 0.5

_UNSET = object()
_face_cascade: Any = _UNSET


def _get_face_cascade() -> Any | None:
    """Lazily load the Haar cascade once per process (workers re-import the module fresh).

    Some OpenCV builds (notably `opencv-python-headless` 5.x) dropped the classic
    `CascadeClassifier` objdetect API and ship no `haarcascades` data at all, in favour of
    the newer DNN-based `FaceDetectorYN`. Rather than pull in a second face-detection
    dependency for a Phase 1 signal, this degrades gracefully: face detection becomes
    unavailable and every photo gets the neutral face component (see `_face_component`),
    the same treatment a genuinely faceless photo gets. The overall guarantee -- a stage
    degrades rather than aborts the run -- matters more here than one extra signal.
    """
    global _face_cascade
    if _face_cascade is _UNSET:
        cascade_cls = getattr(cv2, "CascadeClassifier", None)
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if cascade_cls is None or not cascade_path.exists():
            logger.warning(
                "quality: this OpenCV build has no usable Haar cascade; "
                "face signal will be neutral for every photo."
            )
            _face_cascade = None
        else:
            _face_cascade = cascade_cls(str(cascade_path))
    return _face_cascade


def _sharpness_component(gray: Any) -> float:
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    normalized = variance / gray.size
    return 1.0 - math.exp(-normalized * SHARPNESS_SCALE)


def _exposure_component(gray: Any) -> float:
    shadow_clipped = int(np.count_nonzero(gray <= EXPOSURE_SHADOW_CUTOFF))
    highlight_clipped = int(np.count_nonzero(gray >= EXPOSURE_HIGHLIGHT_CUTOFF))
    clipped_fraction = (shadow_clipped + highlight_clipped) / gray.size
    return 1.0 - clipped_fraction


def _contrast_component(gray: Any) -> float:
    return min(1.0, float(gray.std()) / CONTRAST_MAX_STD)


def _face_signal(gray: Any) -> tuple[int, float]:
    """Return (face_count, largest_face_fraction_of_frame)."""
    cascade = _get_face_cascade()
    if cascade is None:
        return 0, 0.0
    faces = cascade.detectMultiScale(
        gray,
        scaleFactor=FACE_SCALE_FACTOR,
        minNeighbors=FACE_MIN_NEIGHBORS,
        minSize=(FACE_MIN_SIZE_PX, FACE_MIN_SIZE_PX),
    )
    if len(faces) == 0:
        return 0, 0.0
    frame_area = gray.shape[0] * gray.shape[1]
    largest_area = max(int(w) * int(h) for (_, _, w, h) in faces)
    return len(faces), largest_area / frame_area


def _face_component(face_count: int, face_max_frac: float) -> float:
    if face_count == 0:
        return FACE_NEUTRAL_SCORE
    return min(1.0, face_max_frac / FACE_FRAC_SATURATION)


def _overall_score(
    weights: QualityWeights,
    sharpness: float,
    exposure: float,
    contrast: float,
    face_component: float,
) -> float:
    """The documented weighted sum.

    `overall = (w_sharp*sharpness + w_exp*exposure + w_contrast*contrast + w_face*face)
    / (w_sharp + w_exp + w_contrast + w_face)`

    Weights come from `config.quality.weights` and need not sum to 1.0 -- they are
    normalized here, at the point of use.
    """
    total_weight = weights.sharpness + weights.exposure + weights.contrast + weights.face
    if total_weight <= 0:
        return 0.0
    weighted_sum = (
        weights.sharpness * sharpness
        + weights.exposure * exposure
        + weights.contrast * contrast
        + weights.face * face_component
    )
    return weighted_sum / total_weight


def _upsert_quality_score(
    conn: sqlite3.Connection, media_hash: str, payload: dict[str, Any]
) -> None:
    """Write the OpenCV signals, leaving any `content_class` already set alone."""
    conn.execute(
        """
        INSERT INTO score (
            media_hash, sharpness, exposure, contrast, face_count, face_max_frac, overall
        )
        VALUES (
            :media_hash, :sharpness, :exposure, :contrast, :face_count, :face_max_frac, :overall
        )
        ON CONFLICT (media_hash) DO UPDATE SET
            sharpness = excluded.sharpness,
            exposure = excluded.exposure,
            contrast = excluded.contrast,
            face_count = excluded.face_count,
            face_max_frac = excluded.face_max_frac,
            overall = excluded.overall
        """,
        {"media_hash": media_hash, **payload},
    )


def _upsert_content_class(conn: sqlite3.Connection, media_hash: str, content_class: str) -> None:
    """Write `content_class`, leaving any OpenCV signals already set alone."""
    conn.execute(
        """
        INSERT INTO score (media_hash, content_class)
        VALUES (:media_hash, :content_class)
        ON CONFLICT (media_hash) DO UPDATE SET content_class = excluded.content_class
        """,
        {"media_hash": media_hash, "content_class": content_class},
    )


class QualityStage(PerItemStage):
    """Phase 1 deterministic photo quality signals via OpenCV.

    Runs in a worker process (`Executor.PROCESS`): `compute` only touches the file on disk
    and never the DB, so it can cross the process boundary; `persist` runs back in the
    parent and owns the connection.
    """

    name = "quality"
    version = 1
    description = "Sharpness, exposure, contrast, and face-presence signals for photos."
    executor = Executor.PROCESS

    def available(self, ctx: StageContext) -> tuple[bool, str]:
        if cv2 is None:
            return False, "opencv-python-headless is not installed (install the 'vision' extra)"
        return True, ""

    def select(self, ctx: StageContext) -> list[Media]:
        return list(iter_media(ctx.conn))

    def compute(self, media: Media, config: Config) -> dict[str, Any]:
        if media.kind is not MediaKind.IMAGE:
            raise SkipItem("quality scoring only applies to photos, not videos")

        image = cv2.imread(media.path, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"unreadable image: {media.path}")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        sharpness = _sharpness_component(gray)
        exposure = _exposure_component(gray)
        contrast = _contrast_component(gray)
        face_count, face_max_frac = _face_signal(gray)
        face_component = _face_component(face_count, face_max_frac)
        overall = _overall_score(
            config.quality.weights, sharpness, exposure, contrast, face_component
        )

        return {
            "sharpness": sharpness,
            "exposure": exposure,
            "contrast": contrast,
            "face_count": face_count,
            "face_max_frac": face_max_frac,
            "overall": overall,
        }

    def persist(self, ctx: StageContext, media: Media, payload: dict[str, Any]) -> None:
        _upsert_quality_score(ctx.conn, media.hash, payload)


class ContentClassStage(BatchStage):
    """Zero-shot CLIP classification of what a photo actually shows.

    Filtering out screenshots and receipts (`config.quality.reject_content_classes`) before
    selection is one of the highest-value cheap wins in the pipeline. CLIP itself is owned
    by T14 (`story_book.pipeline.embeddings`); this stage only picks the argmax label per
    image and persists it.
    """

    name = "content_class"
    version = 1
    description = "CLIP zero-shot content classification (screenshot/receipt/document/...)."

    def __init__(self) -> None:
        self._runner: ClipRunner | None = None

    def available(self, ctx: StageContext) -> tuple[bool, str]:
        if clip_importable is None:
            return False, "story_book.pipeline.embeddings is not available yet (T14)"
        return clip_importable()

    def select(self, ctx: StageContext) -> list[Media]:
        return [media for media in iter_media(ctx.conn) if media.kind is MediaKind.IMAGE]

    def process_batch(self, ctx: StageContext, batch: list[Media]) -> dict[str, Any]:
        runner = self._runner_for(ctx)
        paths = [Path(media.path) for media in batch]
        classifications = runner.classify(paths, CONTENT_CLASSES)

        results: dict[str, Any] = {}
        for media, probabilities in zip(batch, classifications, strict=True):
            if not probabilities:
                continue
            label = max(probabilities, key=probabilities.get)
            _upsert_content_class(ctx.conn, media.hash, label)
            results[media.hash] = label
        return results

    def _runner_for(self, ctx: StageContext) -> ClipRunner:
        if self._runner is None:
            self._runner = load_clip(ctx.config)
        return self._runner
