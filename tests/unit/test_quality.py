"""Unit tests for Module 8 quality scoring.

No DB, filesystem, or network. OpenCV's pure math functions (Laplacian, cvtColor, ...) are
exercised directly against in-memory numpy arrays -- that's deterministic computation, not
filesystem or network access. Anything that would touch a file (`cv2.imread`) or the CLIP
model (`load_clip`) is mocked.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from story_book.config import Config, QualityWeights
from story_book.db.models import Media, MediaKind
from story_book.pipeline import quality
from story_book.pipeline.base import Executor, SkipItem


def _flat_gray(value: int, size: tuple[int, int] = (24, 24)) -> np.ndarray:
    return np.full(size, value, dtype=np.uint8)


def _noisy_gray(seed: int, size: tuple[int, int] = (24, 24)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=size, dtype=np.uint8)


class TestSharpnessComponent:
    def test_a_sharp_image_scores_higher_than_a_blurred_one(self) -> None:
        sharp = _noisy_gray(seed=1)
        blurred = _flat_gray(128)
        assert quality._sharpness_component(sharp) > quality._sharpness_component(blurred)

    def test_a_perfectly_flat_image_scores_zero(self) -> None:
        assert quality._sharpness_component(_flat_gray(100)) == 0.0

    def test_score_stays_within_unit_range(self) -> None:
        score = quality._sharpness_component(_noisy_gray(seed=2))
        assert 0.0 <= score <= 1.0


class TestExposureComponent:
    def test_a_well_exposed_image_scores_near_one(self) -> None:
        mid_gray = _flat_gray(128)
        assert quality._exposure_component(mid_gray) == pytest.approx(1.0)

    def test_an_overexposed_image_scores_poorly(self) -> None:
        blown_highlights = _flat_gray(250)
        assert quality._exposure_component(blown_highlights) < 0.1

    def test_an_underexposed_image_scores_poorly(self) -> None:
        blown_shadows = _flat_gray(6)
        assert quality._exposure_component(blown_shadows) < 0.1


class TestContrastComponent:
    def test_a_flat_image_has_zero_contrast(self) -> None:
        assert quality._contrast_component(_flat_gray(128)) == 0.0

    def test_a_bimodal_black_and_white_image_saturates_near_one(self) -> None:
        half_and_half = np.zeros((10, 10), dtype=np.uint8)
        half_and_half[:5, :] = 255
        assert quality._contrast_component(half_and_half) == pytest.approx(1.0, abs=1e-6)


class TestFaceSignal:
    def test_no_detections_means_zero_count_and_fraction(self, mocker) -> None:
        cascade = mocker.Mock()
        cascade.detectMultiScale.return_value = []
        mocker.patch.object(quality, "_get_face_cascade", return_value=cascade)
        assert quality._face_signal(_flat_gray(128)) == (0, 0.0)

    def test_largest_face_fraction_is_computed_from_the_biggest_box(self, mocker) -> None:
        cascade = mocker.Mock()
        cascade.detectMultiScale.return_value = [(0, 0, 4, 4), (0, 0, 2, 2)]
        mocker.patch.object(quality, "_get_face_cascade", return_value=cascade)
        gray = _flat_gray(128, size=(20, 20))
        count, frac = quality._face_signal(gray)
        assert count == 2
        assert frac == pytest.approx((4 * 4) / (20 * 20))


class TestFaceComponent:
    def test_no_face_gets_a_neutral_score(self) -> None:
        assert quality._face_component(0, 0.0) == quality.FACE_NEUTRAL_SCORE

    def test_a_large_face_saturates_at_one(self) -> None:
        assert quality._face_component(1, quality.FACE_FRAC_SATURATION * 2) == 1.0

    def test_a_small_face_scores_between_zero_and_one(self) -> None:
        score = quality._face_component(1, quality.FACE_FRAC_SATURATION / 2)
        assert 0.0 < score < 1.0


class TestOverallScore:
    def test_it_matches_the_documented_formula(self) -> None:
        weights = QualityWeights(sharpness=0.4, exposure=0.25, contrast=0.15, face=0.2)
        overall = quality._overall_score(weights, 0.8, 0.6, 0.4, 0.5)
        expected = (0.4 * 0.8 + 0.25 * 0.6 + 0.15 * 0.4 + 0.2 * 0.5) / (0.4 + 0.25 + 0.15 + 0.2)
        assert overall == pytest.approx(expected)

    def test_weights_need_not_sum_to_one(self) -> None:
        weights = QualityWeights(sharpness=4.0, exposure=0.0, contrast=0.0, face=0.0)
        assert quality._overall_score(weights, 0.5, 1.0, 1.0, 1.0) == pytest.approx(0.5)

    def test_all_zero_weights_do_not_divide_by_zero(self) -> None:
        weights = QualityWeights(sharpness=0.0, exposure=0.0, contrast=0.0, face=0.0)
        assert quality._overall_score(weights, 1.0, 1.0, 1.0, 1.0) == 0.0


class TestQualityStageMetadata:
    def test_uses_the_process_executor(self) -> None:
        assert quality.QualityStage.executor is Executor.PROCESS

    def test_name_is_stable(self) -> None:
        assert quality.QualityStage().name == "quality"


class TestQualityStageAvailable:
    def test_unavailable_when_opencv_is_missing(self, mocker) -> None:
        mocker.patch.object(quality, "cv2", None)
        available, reason = quality.QualityStage().available(mocker.Mock())
        assert available is False
        assert "opencv" in reason.lower()

    def test_available_when_opencv_is_present(self, mocker) -> None:
        mocker.patch.object(quality, "cv2", mocker.Mock())
        available, _ = quality.QualityStage().available(mocker.Mock())
        assert available is True


class TestQualityStageCompute:
    def test_videos_are_skipped(self) -> None:
        stage = quality.QualityStage()
        video = Media(hash="h1", path="/x.mp4", kind=MediaKind.VIDEO, bytes=1, mtime=0.0)
        with pytest.raises(SkipItem):
            stage.compute(video, Config())

    def test_unreadable_image_raises(self, mocker) -> None:
        mocker.patch.object(quality.cv2, "imread", return_value=None)
        stage = quality.QualityStage()
        media = Media(hash="h1", path="/nope.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        with pytest.raises(ValueError):
            stage.compute(media, Config())

    def test_a_sharp_image_scores_higher_overall_than_a_blurred_one(self, mocker) -> None:
        stage = quality.QualityStage()
        media = Media(hash="h1", path="/x.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        cascade = mocker.Mock()
        cascade.detectMultiScale.return_value = []
        mocker.patch.object(quality, "_get_face_cascade", return_value=cascade)

        sharp_bgr = np.stack([_noisy_gray(seed=1)] * 3, axis=-1)
        blurred_bgr = np.stack([_flat_gray(128)] * 3, axis=-1)

        mocker.patch.object(quality.cv2, "imread", return_value=sharp_bgr)
        mocker.patch.object(quality.cv2, "cvtColor", side_effect=lambda img, _flag: img[:, :, 0])
        sharp_payload = stage.compute(media, Config())

        mocker.patch.object(quality.cv2, "imread", return_value=blurred_bgr)
        blurred_payload = stage.compute(media, Config())

        assert sharp_payload["overall"] > blurred_payload["overall"]

    def test_payload_has_every_score_field(self, mocker) -> None:
        stage = quality.QualityStage()
        media = Media(hash="h1", path="/x.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        cascade = mocker.Mock()
        cascade.detectMultiScale.return_value = []
        mocker.patch.object(quality, "_get_face_cascade", return_value=cascade)
        bgr = np.stack([_noisy_gray(seed=3)] * 3, axis=-1)
        mocker.patch.object(quality.cv2, "imread", return_value=bgr)
        mocker.patch.object(quality.cv2, "cvtColor", side_effect=lambda img, _flag: img[:, :, 0])

        payload = stage.compute(media, Config())

        assert set(payload) == {
            "sharpness",
            "exposure",
            "contrast",
            "face_count",
            "face_max_frac",
            "overall",
        }


class TestQualityStagePersist:
    def test_it_upserts_via_the_shared_helper(self, mocker) -> None:
        upsert = mocker.patch.object(quality, "_upsert_quality_score")
        stage = quality.QualityStage()
        media = Media(hash="h1", path="/x.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        ctx = mocker.Mock(conn="fake-conn")
        payload = {"sharpness": 0.5}

        stage.persist(ctx, media, payload)

        upsert.assert_called_once_with("fake-conn", "h1", payload)


class TestContentClasses:
    def test_includes_every_reject_class_from_config(self) -> None:
        assert set(Config().quality.reject_content_classes) <= set(quality.CONTENT_CLASSES)

    def test_includes_the_required_phase_one_labels(self) -> None:
        required = {"food", "landscape", "group_photo", "other"}
        assert required <= set(quality.CONTENT_CLASSES)


class TestContentClassStageAvailable:
    def test_unavailable_when_the_embeddings_module_is_missing(self, mocker) -> None:
        mocker.patch.object(quality, "clip_importable", None)
        available, reason = quality.ContentClassStage().available(mocker.Mock())
        assert available is False
        assert reason

    def test_defers_to_clip_importable_when_the_module_is_present(self, mocker) -> None:
        mocker.patch.object(quality, "clip_importable", return_value=(False, "no torch"))
        available, reason = quality.ContentClassStage().available(mocker.Mock())
        assert available is False
        assert reason == "no torch"

    def test_available_when_clip_importable_reports_true(self, mocker) -> None:
        mocker.patch.object(quality, "clip_importable", return_value=(True, ""))
        available, _ = quality.ContentClassStage().available(mocker.Mock())
        assert available is True


class TestContentClassStageSelect:
    def test_only_images_are_selected(self, mocker) -> None:
        photo = Media(hash="p", path="/p.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        video = Media(hash="v", path="/v.mp4", kind=MediaKind.VIDEO, bytes=1, mtime=0.0)
        mocker.patch.object(quality, "iter_media", return_value=iter([photo, video]))
        selected = quality.ContentClassStage().select(mocker.Mock())
        assert selected == [photo]


class TestContentClassStageProcessBatch:
    def test_it_persists_the_top_label_per_image(self, mocker) -> None:
        stage = quality.ContentClassStage()
        fake_runner = mocker.Mock()
        fake_runner.classify.return_value = [
            {"screenshot": 0.9, "other": 0.1},
            {"food": 0.7, "landscape": 0.3},
        ]
        mocker.patch.object(stage, "_runner_for", return_value=fake_runner)
        upsert = mocker.patch.object(quality, "_upsert_content_class")

        one = Media(hash="h1", path="/a.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        two = Media(hash="h2", path="/b.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        ctx = mocker.Mock()

        results = stage.process_batch(ctx, [one, two])

        assert results == {"h1": "screenshot", "h2": "food"}
        upsert.assert_any_call(ctx.conn, "h1", "screenshot")
        upsert.assert_any_call(ctx.conn, "h2", "food")

    def test_an_empty_classification_is_dropped_not_persisted(self, mocker) -> None:
        stage = quality.ContentClassStage()
        fake_runner = mocker.Mock()
        fake_runner.classify.return_value = [{}]
        mocker.patch.object(stage, "_runner_for", return_value=fake_runner)
        upsert = mocker.patch.object(quality, "_upsert_content_class")

        one = Media(hash="h1", path="/a.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        results = stage.process_batch(mocker.Mock(), [one])

        assert results == {}
        upsert.assert_not_called()

    def test_the_clip_runner_is_created_lazily_and_reused(self, mocker) -> None:
        stage = quality.ContentClassStage()
        fake_runner = mocker.Mock()
        fake_runner.classify.return_value = []
        loader = mocker.patch.object(quality, "load_clip", return_value=fake_runner)

        ctx = mocker.Mock()
        stage.process_batch(ctx, [])
        stage.process_batch(ctx, [])

        loader.assert_called_once_with(ctx.config)


class TestSharpnessScoreIsMonotonic:
    def test_more_variance_always_scores_higher(self) -> None:
        low = quality._sharpness_component(_flat_gray(100))
        high = quality._sharpness_component(_noisy_gray(seed=7))
        assert not math.isclose(low, high)
        assert high > low
