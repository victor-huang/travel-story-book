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

from PIL import Image  # hard dependency, and the only loader that handles HEIC

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
except ImportError:  # pragma: no cover - the vision extra may not be installed
    ClipRunner = None  # type: ignore[assignment,misc]
    clip_importable = None  # type: ignore[assignment]
    load_clip = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# --- content classification ------------------------------------------------------------

CONTENT_CLASS_PROMPTS: dict[str, tuple[str, ...]] = {
    "screenshot": (
        "a screenshot of a phone screen",
        "a screenshot of a computer user interface",
        "a screenshot of an app or a web page",
    ),
    "receipt": (
        "a photo of a paper receipt",
        "a photo of a restaurant bill",
    ),
    "document": (
        "a photo of a printed document",
        "a scan of a page of text",
        "a photo of a ticket or boarding pass",
    ),
    "food": (
        "a photo of a plate of food",
        "a photo of a meal at a restaurant",
    ),
    "landscape": (
        "a travel photo of an outdoor scene",
        "a travel photo of a building or landmark",
        "a photo of a city street",
        "a photo of a garden or park",
    ),
    "group_photo": (
        "a photo of a group of people posing",
        "a portrait photo of a person",
    ),
    "other": (
        "a photo of an object",
        "a snapshot of something ordinary",
    ),
}
"""Natural-language prompts per class, and the reason they are sentences rather than words.

CLIP is trained on image/caption pairs, so its text tower expects captions. Passing bare label
tokens -- `"screenshot"`, `"landscape"`, and worst of all `"group_photo"` with an underscore --
produces poor text embeddings and a wildly biased result: on a real 277-photo travel library the
bare-word version labelled **209 of them "screenshot"**, which (since `screenshot` is a rejected
class) would have thrown out three quarters of the trip.

The fixture tests did not catch it because they only checked that a real screenshot classifies as
a screenshot. True-positive precision was fine; the false-positive rate on ordinary photos was
never measured.

Several prompts per class, with their probabilities summed, is the standard prompt-ensembling
trick and is more robust than picking one phrasing.
"""

CONTENT_CLASSES: tuple[str, ...] = tuple(CONTENT_CLASS_PROMPTS)
"""Canonical class names -- what lands in `score.content_class`.

Must be a superset of `config.quality.reject_content_classes` (screenshot/receipt/document by
default).
"""

# --- sharpness --------------------------------------------------------------------------

SHARPNESS_WORKING_EDGE = 512
"""Short edge, in pixels, that sharpness is measured at.

Fixed so resolution cannot skew the measure: downscaling removes the high-frequency content
sharpness is made of, so images must be compared at a common size. Dividing by pixel count --
the intuitive normalization -- is wrong and was the original bug; see `_sharpness_component`.
"""

SHARPNESS_REFERENCE_VARIANCE = 1500.0
"""Laplacian variance mapping to ~0.63 on the saturating curve.

Calibrated against a real 277-photo library, where variance at a 512px short edge ran 184 to
4746 with a median of 2022. Changing it rescales the spread; it never changes ordering.
"""

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

# Detector knobs, not business thresholds. Kept for reference by the YuNet detector; OpenCV 5
# dropped the Haar cascade API these originally configured.
FACE_SCALE_FACTOR = 1.1
FACE_MIN_NEIGHBORS = 5
FACE_MIN_SIZE_PX = 24

# A face filling this fraction of the frame or more saturates the face-presence score at 1.0.
FACE_FRAC_SATURATION = 0.15

# Absence of a face is not evidence of a bad photo -- many travel highlights are landscapes
# or food with nobody in frame -- so a faceless photo gets a neutral score rather than 0.
FACE_NEUTRAL_SCORE = 0.5

_UNSET = object()
_face_detector: Any = _UNSET


def face_detection_available(config: Config) -> bool:
    """Whether a usable face detector is configured and loadable."""
    return _get_face_detector(config) is not None


def _get_face_detector(config: Config) -> Any | None:
    """Lazily build a YuNet detector once per process (pool workers re-import fresh).

    OpenCV 5.x removed the classic `CascadeClassifier` objdetect API and ships no detection
    data at all, in favour of the DNN `FaceDetectorYN`. That needs a small ONNX model, which
    this project does **not** download on demand: the standing promise is that only the landmark
    stage touches the network, and `--no-cloud` must complete the whole pipeline. So the model
    path is explicit configuration (`models.face_detector_model`).

    When no detector is available the face signal is *dropped from the score entirely* rather
    than substituted with a neutral constant -- see `_overall_score`. A constant contributes the
    same value to every photo while still consuming its share of the weight, which silently
    dilutes the signals that do work.
    """
    global _face_detector
    if _face_detector is not _UNSET:
        return _face_detector

    model_path = config.models.face_detector_model
    detector_cls = getattr(cv2, "FaceDetectorYN", None)
    if model_path is None or detector_cls is None:
        if detector_cls is None:
            logger.warning("quality: this OpenCV build has no FaceDetectorYN; face signal off.")
        _face_detector = None
        return None

    path = Path(model_path).expanduser()
    if not path.exists():
        logger.warning("quality: face detector model not found at %s; face signal off.", path)
        _face_detector = None
        return None

    try:
        _face_detector = detector_cls.create(str(path), "", (320, 320), 0.9, 0.3, 5000)
    except cv2.error as exc:
        logger.warning("quality: could not load face detector (%s); face signal off.", exc)
        _face_detector = None
    return _face_detector


def _load_bgr(path: str) -> Any:
    """Load any format the scanner accepts, as a BGR array for OpenCV.

    Deliberately not `cv2.imread`: OpenCV cannot read HEIC at all, and registering
    pillow-heif only teaches *Pillow* the format. Since HEIC is the dominant iPhone format, an
    imread-based path silently failed every one of them. Pillow reads everything in the
    scanner's allowlist, so it is the single loading path.
    """
    try:
        with Image.open(path) as handle:
            rgb = handle.convert("RGB")
            return np.asarray(rgb)[:, :, ::-1].copy()
    except Exception as exc:
        # Wrapped so the recorded stage_result error names the file rather than surfacing a bare
        # FileNotFoundError or UnidentifiedImageError from deep inside Pillow.
        raise ValueError(f"unreadable image: {path}") from exc


def _sharpness_component(gray: Any) -> float:
    """Blur measure: variance of the Laplacian, evaluated at a fixed working resolution.

    Two things were wrong with the obvious version.

    Dividing by pixel count -- as a "resolution normalization" -- destroys the measure. Laplacian
    variance is a *per-pixel* statistic that does not grow with image size, so dividing a typical
    variance of ~2000 by 12 million pixels drove every real photo to ~0.001. On a real 277-photo
    library the whole sharpness signal had a standard deviation of 0.009 while carrying the
    largest weight in the score, which left `overall` compressed into a 0.13-0.42 band with no
    discriminating power. The same-size fixture comparison still ordered correctly, which is why
    the unit tests passed: the bug preserved *ordering* while destroying *range*.

    Resolution still has to be handled, because downscaling removes exactly the high-frequency
    content this measures. So the image is resized to a fixed short edge first and the variance is
    normalized against a reference constant. Images already smaller than that edge are measured as
    they are -- upscaling invents no detail.

    Calibrated against the real library: at a 512px short edge the observed variance ran 184
    (blurriest) to 4746 (sharpest) with a median of 2022, so a 1500 reference puts typical photos
    in the middle of the curve rather than pinned at either end.
    """
    working = _resize_short_edge(gray, SHARPNESS_WORKING_EDGE)
    variance = float(cv2.Laplacian(working, cv2.CV_64F).var())
    return 1.0 - math.exp(-variance / SHARPNESS_REFERENCE_VARIANCE)


def _resize_short_edge(gray: Any, edge: int) -> Any:
    short = min(gray.shape[:2])
    if short <= edge:
        return gray
    scale = edge / short
    size = (max(1, int(gray.shape[1] * scale)), max(1, int(gray.shape[0] * scale)))
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA)


def _exposure_component(gray: Any) -> float:
    shadow_clipped = int(np.count_nonzero(gray <= EXPOSURE_SHADOW_CUTOFF))
    highlight_clipped = int(np.count_nonzero(gray >= EXPOSURE_HIGHLIGHT_CUTOFF))
    clipped_fraction = (shadow_clipped + highlight_clipped) / gray.size
    return 1.0 - clipped_fraction


def _contrast_component(gray: Any) -> float:
    return min(1.0, float(gray.std()) / CONTRAST_MAX_STD)


def _face_signal(image: Any, config: Config) -> tuple[int, float] | None:
    """(face_count, largest_face_fraction), or None when no detector is available.

    None is deliberately distinct from (0, 0.0): "we could not look" is not "there is nobody
    here", and conflating them is what let a missing detector masquerade as a real measurement.
    """
    detector = _get_face_detector(config)
    if detector is None:
        return None

    height, width = image.shape[:2]
    detector.setInputSize((width, height))
    try:
        _, faces = detector.detect(image)
    except cv2.error:
        return None
    if faces is None or len(faces) == 0:
        return 0, 0.0

    frame_area = float(width * height)
    largest = max(float(face[2]) * float(face[3]) for face in faces)
    return len(faces), (largest / frame_area if frame_area else 0.0)


def _face_component(signal: tuple[int, float] | None) -> float | None:
    """None propagates "no detector" through to the score, which drops the term entirely."""
    if signal is None:
        return None
    face_count, face_max_frac = signal
    if face_count == 0:
        return FACE_NEUTRAL_SCORE
    return min(1.0, face_max_frac / FACE_FRAC_SATURATION)


def _overall_score(
    weights: QualityWeights,
    sharpness: float,
    exposure: float,
    contrast: float,
    face_component: float | None,
) -> float:
    """The documented weighted sum, normalized over the components actually measured.

    `overall = sum(w_i * s_i) / sum(w_i)` across available components only.

    When `face_component` is None (no detector configured) the face term is dropped from *both*
    numerator and denominator. Substituting a neutral constant instead would let a fifth of the
    score -- the default face weight -- be identical for every photo while still consuming its
    share, diluting the signals that do discriminate. Dropping it keeps the score a weighted
    average of real measurements.
    """
    terms = [
        (weights.sharpness, sharpness),
        (weights.exposure, exposure),
        (weights.contrast, contrast),
    ]
    if face_component is not None:
        terms.append((weights.face, face_component))

    total_weight = sum(weight for weight, _ in terms)
    if total_weight <= 0:
        return 0.0
    return sum(weight * value for weight, value in terms) / total_weight


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

        image = _load_bgr(media.path)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        sharpness = _sharpness_component(gray)
        exposure = _exposure_component(gray)
        contrast = _contrast_component(gray)
        # YuNet takes the colour image; the other signals are grayscale.
        face_signal = _face_signal(image, config)
        face_component = _face_component(face_signal)
        overall = _overall_score(
            config.quality.weights, sharpness, exposure, contrast, face_component
        )

        return {
            "sharpness": sharpness,
            "exposure": exposure,
            "contrast": contrast,
            # NULL rather than 0 when no detector ran: "not measured" is not "no faces".
            "face_count": face_signal[0] if face_signal is not None else None,
            "face_max_frac": face_signal[1] if face_signal is not None else None,
            "overall": overall,
        }

    def persist(self, ctx: StageContext, media: Media, payload: dict[str, Any]) -> None:
        _upsert_quality_score(ctx.conn, media.hash, payload)


def _classify_paths(runner: ClipRunner, paths: list[Path]) -> list[dict[str, float]]:
    """Zero-shot classify, aggregating each class's prompts back into one probability.

    The softmax runs across every prompt, so a class's probability is the sum over its own
    prompts -- P(class) = sum of P(prompt) for its prompts.
    """
    prompts: list[str] = []
    owners: list[str] = []
    for label, label_prompts in CONTENT_CLASS_PROMPTS.items():
        for prompt in label_prompts:
            prompts.append(prompt)
            owners.append(label)

    aggregated: list[dict[str, float]] = []
    for row in runner.classify(paths, prompts):
        totals = dict.fromkeys(CONTENT_CLASS_PROMPTS, 0.0)
        for owner, prompt in zip(owners, prompts, strict=True):
            totals[owner] += row.get(prompt, 0.0)
        # An unusable response must stay empty rather than becoming all-zeros: `max()` over
        # all-zeros silently returns the first class, which is how "no answer" would have been
        # recorded as "screenshot" -- the same rejected label this stage already got wrong once.
        aggregated.append(totals if any(totals.values()) else {})
    return aggregated


def _classify_resiliently(
    runner: ClipRunner, batch: list[Media]
) -> list[tuple[Media, dict[str, float]]]:
    """Classify a batch, degrading to per-item on failure.

    Same reasoning as the embedding stage: one unreadable file raised inside `classify` would
    fail every co-batched item. Try the batch, then retry individually so only the broken file
    fails; omitted items are recorded as per-item failures by the runner.
    """
    paths = [Path(media.path) for media in batch]
    try:
        return list(zip(batch, _classify_paths(runner, paths), strict=True))
    except Exception:
        logger.warning(
            "content_class: batch of %d failed; retrying individually", len(batch), exc_info=True
        )

    recovered: list[tuple[Media, dict[str, float]]] = []
    for media in batch:
        try:
            recovered.append((media, _classify_paths(runner, [Path(media.path)])[0]))
        except Exception:
            logger.warning("content_class: %s could not be classified", media.path, exc_info=True)
    return recovered


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
        results: dict[str, Any] = {}
        for media, probabilities in _classify_resiliently(runner, batch):
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
