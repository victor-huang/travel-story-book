"""Unit tests for Module 8 quality scoring.

No DB, filesystem, or network. OpenCV's pure math functions (Laplacian, cvtColor, ...) are
exercised directly against in-memory numpy arrays -- that's deterministic computation, not
filesystem or network access. Anything that would touch a file (`cv2.imread`) or the CLIP
model (`load_clip`) is mocked.
"""

from __future__ import annotations

import math
from pathlib import Path

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
    def test_no_detector_returns_none(self, mocker) -> None:
        """None means "could not look", which is not the same as "nobody is here"."""
        mocker.patch.object(quality, "_get_face_detector", return_value=None)
        assert quality._face_signal(_flat_gray(128), Config()) is None

    def test_no_detections_means_zero_count_and_fraction(self, mocker) -> None:
        detector = mocker.Mock()
        detector.detect.return_value = (1, None)
        mocker.patch.object(quality, "_get_face_detector", return_value=detector)
        assert quality._face_signal(_flat_gray(128), Config()) == (0, 0.0)

    def test_largest_face_fraction_is_computed_from_the_biggest_box(self, mocker) -> None:
        detector = mocker.Mock()
        detector.detect.return_value = (1, [(0, 0, 4, 4), (0, 0, 2, 2)])
        mocker.patch.object(quality, "_get_face_detector", return_value=detector)
        count, frac = quality._face_signal(_flat_gray(128, size=(20, 20)), Config())
        assert count == 2
        assert frac == pytest.approx((4 * 4) / (20 * 20))

    def test_a_detector_error_degrades_to_none(self, mocker) -> None:
        detector = mocker.Mock()
        detector.detect.side_effect = quality.cv2.error("boom")
        mocker.patch.object(quality, "_get_face_detector", return_value=detector)
        assert quality._face_signal(_flat_gray(128), Config()) is None


class TestFaceDetectorLoading:
    def test_unset_model_path_means_no_detector(self) -> None:
        mocker_free_config = Config()
        assert mocker_free_config.models.face_detector_model is None
        quality._face_detector = quality._UNSET
        assert quality._get_face_detector(mocker_free_config) is None

    def test_a_missing_model_file_means_no_detector(self, mocker) -> None:
        config = Config.from_dict({"models": {"face_detector_model": "/nope/yunet.onnx"}})
        quality._face_detector = quality._UNSET
        assert quality._get_face_detector(config) is None

    def test_availability_helper_reports_false_without_a_model(self) -> None:
        quality._face_detector = quality._UNSET
        assert quality.face_detection_available(Config()) is False


class TestFaceComponent:
    def test_no_detector_propagates_none(self) -> None:
        assert quality._face_component(None) is None

    def test_no_face_gets_a_neutral_score(self) -> None:
        assert quality._face_component((0, 0.0)) == quality.FACE_NEUTRAL_SCORE

    def test_a_large_face_saturates_at_one(self) -> None:
        assert quality._face_component((1, quality.FACE_FRAC_SATURATION * 2)) == 1.0

    def test_a_small_face_scores_between_zero_and_one(self) -> None:
        score = quality._face_component((1, quality.FACE_FRAC_SATURATION / 2))
        assert 0.0 < score < 1.0


class TestOverallScoreDropsUnmeasuredComponents:
    """A missing face detector must not contribute a constant while consuming its weight."""

    def test_none_face_is_dropped_from_the_denominator(self) -> None:
        weights = QualityWeights(sharpness=0.4, exposure=0.25, contrast=0.15, face=0.2)
        overall = quality._overall_score(weights, 0.8, 0.6, 0.4, None)
        expected = (0.4 * 0.8 + 0.25 * 0.6 + 0.15 * 0.4) / (0.4 + 0.25 + 0.15)
        assert overall == pytest.approx(expected)

    def test_a_perfect_photo_still_scores_one_without_a_face_detector(self) -> None:
        """With a neutral constant instead, the ceiling would drop to 0.9 -- unreachable."""
        weights = QualityWeights(sharpness=0.4, exposure=0.25, contrast=0.15, face=0.2)
        assert quality._overall_score(weights, 1.0, 1.0, 1.0, None) == pytest.approx(1.0)

    def test_face_weight_still_counts_when_measured(self) -> None:
        weights = QualityWeights(sharpness=0.4, exposure=0.25, contrast=0.15, face=0.2)
        with_face = quality._overall_score(weights, 0.5, 0.5, 0.5, 1.0)
        without = quality._overall_score(weights, 0.5, 0.5, 0.5, None)
        assert with_face > without


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
        mocker.patch.object(
            quality, "_load_bgr", side_effect=ValueError("unreadable image: /x.jpg")
        )
        stage = quality.QualityStage()
        media = Media(hash="h1", path="/nope.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        with pytest.raises(ValueError):
            stage.compute(media, Config())

    def test_a_sharp_image_scores_higher_overall_than_a_blurred_one(self, mocker) -> None:
        stage = quality.QualityStage()
        media = Media(hash="h1", path="/x.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        detector = mocker.Mock()
        detector.detect.return_value = (1, None)
        mocker.patch.object(quality, "_get_face_detector", return_value=detector)

        sharp_bgr = np.stack([_noisy_gray(seed=1)] * 3, axis=-1)
        blurred_bgr = np.stack([_flat_gray(128)] * 3, axis=-1)

        mocker.patch.object(quality, "_load_bgr", return_value=sharp_bgr)
        mocker.patch.object(quality.cv2, "cvtColor", side_effect=lambda img, _flag: img[:, :, 0])
        sharp_payload = stage.compute(media, Config())

        mocker.patch.object(quality, "_load_bgr", return_value=blurred_bgr)
        blurred_payload = stage.compute(media, Config())

        assert sharp_payload["overall"] > blurred_payload["overall"]

    def test_payload_has_every_score_field(self, mocker) -> None:
        stage = quality.QualityStage()
        media = Media(hash="h1", path="/x.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        detector = mocker.Mock()
        detector.detect.return_value = (1, None)
        mocker.patch.object(quality, "_get_face_detector", return_value=detector)
        bgr = np.stack([_noisy_gray(seed=3)] * 3, axis=-1)
        mocker.patch.object(quality, "_load_bgr", return_value=bgr)
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
        # classify() speaks prompts now, so favour a class by weighting its own prompts.
        fake_runner.classify.return_value = [
            _prompt_scores("screenshot"),
            _prompt_scores("food"),
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
        fake_runner.classify.return_value = [{}]  # provider returned nothing usable
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


class TestContentClassPrompts:
    """Guards the defect directly: bare-word labels labelled 209/277 real travel photos
    "screenshot", which would have rejected three quarters of a trip from highlights.
    """

    def test_every_class_has_at_least_one_prompt(self) -> None:
        assert all(quality.CONTENT_CLASS_PROMPTS[c] for c in quality.CONTENT_CLASSES)

    def test_prompts_are_natural_language_not_bare_labels(self) -> None:
        """A prompt without a space is a bare token, which is what caused the miscalibration."""
        for prompts in quality.CONTENT_CLASS_PROMPTS.values():
            assert all(" " in prompt for prompt in prompts)

    def test_no_prompt_is_just_the_class_name(self) -> None:
        for label, prompts in quality.CONTENT_CLASS_PROMPTS.items():
            assert label not in prompts

    def test_class_names_come_from_the_prompt_map(self) -> None:
        assert tuple(quality.CONTENT_CLASS_PROMPTS) == quality.CONTENT_CLASSES

    def test_every_rejected_class_is_classifiable(self) -> None:
        assert set(Config().quality.reject_content_classes) <= set(quality.CONTENT_CLASSES)

    def test_probabilities_are_aggregated_per_class(self, mocker) -> None:
        """Each class's probability is the sum over its own prompts."""
        runner = mocker.Mock()
        prompt_count = sum(len(p) for p in quality.CONTENT_CLASS_PROMPTS.values())
        runner.classify.return_value = [dict.fromkeys(_all_prompts(), 1.0 / prompt_count)]

        result = quality._classify_paths(runner, [Path("/x.jpg")])[0]

        expected = len(quality.CONTENT_CLASS_PROMPTS["screenshot"]) / prompt_count
        assert result["screenshot"] == pytest.approx(expected)

    def test_aggregated_probabilities_sum_to_one(self, mocker) -> None:
        runner = mocker.Mock()
        prompt_count = sum(len(p) for p in quality.CONTENT_CLASS_PROMPTS.values())
        runner.classify.return_value = [dict.fromkeys(_all_prompts(), 1.0 / prompt_count)]

        result = quality._classify_paths(runner, [Path("/x.jpg")])[0]
        assert sum(result.values()) == pytest.approx(1.0)

    def test_the_winning_class_is_the_one_with_the_strongest_prompts(self, mocker) -> None:
        runner = mocker.Mock()
        scores = dict.fromkeys(_all_prompts(), 0.0)
        for prompt in quality.CONTENT_CLASS_PROMPTS["landscape"]:
            scores[prompt] = 0.25
        runner.classify.return_value = [scores]

        result = quality._classify_paths(runner, [Path("/x.jpg")])[0]
        assert max(result, key=result.get) == "landscape"


def _all_prompts() -> list[str]:
    return [p for prompts in quality.CONTENT_CLASS_PROMPTS.values() for p in prompts]


def _prompt_scores(winner: str) -> dict[str, float]:
    """A classify() row keyed by prompt, with all probability on `winner`'s prompts."""
    scores = dict.fromkeys(_all_prompts(), 0.0)
    for prompt in quality.CONTENT_CLASS_PROMPTS[winner]:
        scores[prompt] = 1.0 / len(quality.CONTENT_CLASS_PROMPTS[winner])
    return scores


class TestUnusableClassifications:
    """An unusable response must be dropped, not silently resolved to the first class.

    Aggregating prompts into classes introduced this: an empty row became all-zero totals, and
    `max()` over all-zeros returns whichever class is first -- `screenshot`, a *rejected* class.
    "No answer" must never become "reject this photo".
    """

    def test_an_empty_response_yields_no_classification(self, mocker) -> None:
        runner = mocker.Mock()
        runner.classify.return_value = [{}]
        assert quality._classify_paths(runner, [Path("/x.jpg")]) == [{}]

    def test_an_all_zero_response_yields_no_classification(self, mocker) -> None:
        runner = mocker.Mock()
        runner.classify.return_value = [dict.fromkeys(_all_prompts(), 0.0)]
        assert quality._classify_paths(runner, [Path("/x.jpg")]) == [{}]

    def test_a_usable_response_is_kept(self, mocker) -> None:
        runner = mocker.Mock()
        runner.classify.return_value = [_prompt_scores("landscape")]
        assert quality._classify_paths(runner, [Path("/x.jpg")])[0] != {}


class TestSharpnessCalibration:
    """Guards the collapse: dividing Laplacian variance by pixel count drove every real photo to
    ~0.001, so sharpness carried the largest weight while contributing no signal. Ordering tests
    could not catch it -- the bug preserved ordering and destroyed range.
    """

    def test_a_detailed_large_image_is_not_pinned_near_zero(self) -> None:
        """The exact failure: a 12-megapixel photo scoring 0.001 instead of ~0.7."""
        large = _noisy_gray(seed=5, size=(3000, 4000))
        assert quality._sharpness_component(large) > 0.5

    def test_score_is_stable_across_resolutions_of_the_same_content(self) -> None:
        """Resolution must not decide the score; that is what the fixed working edge is for."""
        big = _noisy_gray(seed=7, size=(2048, 2048))
        small = quality.cv2.resize(big, (1024, 1024), interpolation=quality.cv2.INTER_AREA)
        assert quality._sharpness_component(big) == pytest.approx(
            quality._sharpness_component(small), abs=0.15
        )

    def test_a_flat_image_scores_near_zero(self) -> None:
        assert quality._sharpness_component(_flat_gray(128, size=(1000, 1000))) < 0.05

    def test_detail_still_outscores_flatness_at_large_sizes(self) -> None:
        detailed = _noisy_gray(seed=11, size=(2000, 2000))
        flat = _flat_gray(128, size=(2000, 2000))
        assert quality._sharpness_component(detailed) > quality._sharpness_component(flat)

    def test_the_working_edge_downscales_only_larger_images(self) -> None:
        small = _noisy_gray(seed=13, size=(200, 200))
        assert quality._resize_short_edge(small, 512).shape == small.shape

    def test_a_larger_image_is_downscaled_to_the_working_edge(self) -> None:
        big = _noisy_gray(seed=17, size=(1500, 3000))
        assert min(quality._resize_short_edge(big, 512).shape[:2]) == 512

    def test_downscaling_preserves_aspect_ratio(self) -> None:
        big = _noisy_gray(seed=19, size=(1000, 2000))
        resized = quality._resize_short_edge(big, 500)
        assert resized.shape[1] / resized.shape[0] == pytest.approx(2.0, abs=0.05)
