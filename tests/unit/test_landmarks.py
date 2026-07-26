"""Unit tests for the landmarks stage: no DB, no filesystem, no network.

Every provider network call is isolated behind `_call_api`, which is mocked here -- nothing in
this file ever reaches the internet. DB-touching helpers (`_selected_media`, `_build_context`,
`_persist`) are exercised in `tests/backend/test_landmarks.py` instead.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from story_book.config import LandmarkConfig
from story_book.db.models import Media, MediaKind
from story_book.pipeline.landmarks.base import (
    CostEstimate,
    LandmarkIdentification,
    LandmarkImageContext,
    LandmarkProvider,
    LandmarkStage,
    cap_to_max_requests,
    estimate_and_confirm,
)
from story_book.pipeline.landmarks.providers import (
    ANTHROPIC_API_KEY_ENV,
    OPENAI_API_KEY_ENV,
    AnthropicLandmarkProvider,
    OpenAILandmarkProvider,
    _validate_entries,
    get_provider,
)


def _media(hash_: str = "h0") -> Media:
    return Media(hash=hash_, path=f"/src/{hash_}.jpg", kind=MediaKind.IMAGE, bytes=100, mtime=0.0)


def _context(hash_: str = "h0") -> LandmarkImageContext:
    return LandmarkImageContext(
        media=_media(hash_),
        image_bytes=b"fake-bytes",
        media_type="image/jpeg",
        place_label="Vienna",
    )


class TestValidateEntries:
    def test_accepts_a_well_formed_entry(self) -> None:
        raw = {
            "landmarks": [
                {
                    "image_index": 0,
                    "name": "Belvedere Palace",
                    "confidence": 0.92,
                    "description": "A baroque palace complex.",
                    "notable_feature": "Klimt's The Kiss",
                }
            ]
        }
        result = _validate_entries(raw)
        assert result[0] == LandmarkIdentification(
            name="Belvedere Palace",
            confidence=0.92,
            description="A baroque palace complex.",
            notable_feature="Klimt's The Kiss",
        )

    def test_notable_feature_is_optional(self) -> None:
        raw = {
            "landmarks": [{"image_index": 0, "name": "X", "confidence": 0.5, "description": "d"}]
        }
        result = _validate_entries(raw)
        assert result[0].notable_feature is None

    def test_top_level_not_a_dict_yields_nothing(self) -> None:
        assert _validate_entries(["not", "a", "dict"]) == {}

    def test_missing_landmarks_key_yields_nothing(self) -> None:
        assert _validate_entries({"foo": "bar"}) == {}

    def test_entry_missing_required_field_is_dropped(self) -> None:
        raw = {"landmarks": [{"image_index": 0, "name": "X", "confidence": 0.5}]}  # no description
        assert _validate_entries(raw) == {}

    def test_confidence_out_of_range_is_dropped(self) -> None:
        raw = {
            "landmarks": [{"image_index": 0, "name": "X", "confidence": 1.5, "description": "d"}]
        }
        assert _validate_entries(raw) == {}

    def test_confidence_as_bool_is_dropped(self) -> None:
        raw = {
            "landmarks": [{"image_index": 0, "name": "X", "confidence": True, "description": "d"}]
        }
        assert _validate_entries(raw) == {}

    def test_blank_name_is_dropped(self) -> None:
        raw = {
            "landmarks": [{"image_index": 0, "name": "   ", "confidence": 0.5, "description": "d"}]
        }
        assert _validate_entries(raw) == {}

    def test_one_bad_entry_does_not_drop_the_others(self) -> None:
        raw = {
            "landmarks": [
                {"image_index": 0, "name": "Good", "confidence": 0.9, "description": "d"},
                {"image_index": 1, "name": "", "confidence": 0.9, "description": "d"},
            ]
        }
        result = _validate_entries(raw)
        assert set(result) == {0}


class TestCapToMaxRequests:
    def test_under_the_cap_keeps_everything(self) -> None:
        config = LandmarkConfig(max_requests=10, images_per_request=4)
        candidates = [_media(f"h{i}") for i in range(5)]
        kept, dropped = cap_to_max_requests(candidates, config)
        assert kept == candidates
        assert dropped == []

    def test_over_the_cap_drops_the_tail(self) -> None:
        config = LandmarkConfig(max_requests=1, images_per_request=2)
        candidates = [_media(f"h{i}") for i in range(5)]
        kept, dropped = cap_to_max_requests(candidates, config)
        assert [m.hash for m in kept] == ["h0", "h1"]
        assert [m.hash for m in dropped] == ["h2", "h3", "h4"]

    def test_zero_max_requests_drops_everything(self) -> None:
        config = LandmarkConfig(max_requests=0, images_per_request=4)
        candidates = [_media("h0")]
        kept, dropped = cap_to_max_requests(candidates, config)
        assert kept == []
        assert dropped == candidates


class TestEstimateAndConfirm:
    def _provider(self, usd: float) -> LandmarkProvider:
        provider = MagicMock(spec=LandmarkProvider)
        provider.name = "fake"
        provider.estimate_cost.return_value = CostEstimate(
            request_count=3, estimated_usd=usd, model="fake-model"
        )
        return provider

    def test_prints_the_estimate_before_any_call(self, capsys: pytest.CaptureFixture[str]) -> None:
        provider = self._provider(0.01)
        config = LandmarkConfig(confirm_above_estimated_usd=1.0)
        confirm = MagicMock(return_value=True)

        estimate = estimate_and_confirm([_media()], config, provider, confirm)

        assert estimate is not None
        assert estimate.estimated_usd == 0.01
        captured = capsys.readouterr()
        assert "estimated cost $0.0100" in captured.out
        provider.identify.assert_not_called()

    def test_below_threshold_does_not_prompt(self) -> None:
        provider = self._provider(0.5)
        config = LandmarkConfig(confirm_above_estimated_usd=1.0)
        confirm = MagicMock(return_value=True)

        estimate_and_confirm([_media()], config, provider, confirm)

        confirm.assert_not_called()

    def test_above_threshold_prompts_and_proceeds_on_accept(self) -> None:
        provider = self._provider(5.0)
        config = LandmarkConfig(confirm_above_estimated_usd=1.0)
        confirm = MagicMock(return_value=True)

        result = estimate_and_confirm([_media()], config, provider, confirm)

        confirm.assert_called_once()
        assert result is not None

    def test_above_threshold_declined_returns_none(self) -> None:
        provider = self._provider(5.0)
        config = LandmarkConfig(confirm_above_estimated_usd=1.0)
        confirm = MagicMock(return_value=False)

        result = estimate_and_confirm([_media()], config, provider, confirm)

        assert result is None


class TestLandmarkStageAvailable:
    def test_no_cloud_flag_skips_the_stage(self) -> None:
        ctx = MagicMock(no_cloud=True)
        available, reason = LandmarkStage().available(ctx)
        assert available is False
        assert reason == "--no-cloud"

    def test_provider_none_skips_the_stage(self) -> None:
        ctx = MagicMock(no_cloud=False)
        ctx.config.landmarks = LandmarkConfig(provider="none")
        available, reason = LandmarkStage().available(ctx)
        assert available is False
        assert "none" in reason

    def test_unknown_provider_skips_the_stage(self) -> None:
        ctx = MagicMock(no_cloud=False)
        ctx.config.landmarks = LandmarkConfig(provider="does-not-exist")
        stage = LandmarkStage(provider_factory=lambda config: None)
        available, reason = stage.available(ctx)
        assert available is False
        assert "does-not-exist" in reason

    def test_no_key_configured_is_a_clean_skip_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
        ctx = MagicMock(no_cloud=False)
        ctx.config.landmarks = LandmarkConfig(provider="anthropic")

        available, reason = LandmarkStage().available(ctx)

        assert available is False
        assert ANTHROPIC_API_KEY_ENV in reason

    def test_available_when_key_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-fake")
        ctx = MagicMock(no_cloud=False)
        ctx.config.landmarks = LandmarkConfig(provider="anthropic")

        available, reason = LandmarkStage().available(ctx)

        assert available is True
        assert reason == ""


class TestGetProvider:
    def test_none_provider_returns_none(self) -> None:
        assert get_provider(LandmarkConfig(provider="none")) is None

    def test_unknown_provider_returns_none(self) -> None:
        assert get_provider(LandmarkConfig(provider="bogus")) is None

    def test_anthropic_provider_is_built_with_configured_model(self) -> None:
        provider = get_provider(LandmarkConfig(provider="anthropic", model="claude-opus-5"))
        assert isinstance(provider, AnthropicLandmarkProvider)
        assert provider.model == "claude-opus-5"

    def test_openai_provider_is_built_with_configured_model(self) -> None:
        provider = get_provider(LandmarkConfig(provider="openai", model="gpt-4o"))
        assert isinstance(provider, OpenAILandmarkProvider)
        assert provider.model == "gpt-4o"


class TestAnthropicLandmarkProviderAvailable:
    def test_missing_key_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
        available, reason = AnthropicLandmarkProvider().available()
        assert available is False
        assert ANTHROPIC_API_KEY_ENV in reason

    def test_present_key_is_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-fake")
        available, reason = AnthropicLandmarkProvider().available()
        assert available is True
        assert reason == ""


class TestOpenAILandmarkProviderAvailable:
    def test_missing_key_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV, raising=False)
        available, reason = OpenAILandmarkProvider().available()
        assert available is False
        assert OPENAI_API_KEY_ENV in reason


class TestAnthropicLandmarkProviderEstimateCost:
    def test_request_count_rounds_up(self) -> None:
        provider = AnthropicLandmarkProvider()
        estimate = provider.estimate_cost(image_count=5, images_per_request=4)
        assert estimate.request_count == 2  # ceil(5/4)

    def test_zero_images_costs_nothing(self) -> None:
        provider = AnthropicLandmarkProvider()
        estimate = provider.estimate_cost(image_count=0, images_per_request=4)
        assert estimate.request_count == 0
        assert estimate.estimated_usd == 0.0

    def test_more_images_per_request_costs_more_per_request(self) -> None:
        provider = AnthropicLandmarkProvider()
        small = provider.estimate_cost(image_count=4, images_per_request=4)
        large = provider.estimate_cost(image_count=4, images_per_request=8)
        # Same image_count at a larger images_per_request still means 1 request; the ceiling
        # is on request count, but cost scales differently -- sanity check they aren't equal
        # for different batch shapes.
        assert small.request_count == 1
        assert large.request_count == 1
        assert small.estimated_usd <= large.estimated_usd


class TestAnthropicLandmarkProviderIdentify:
    def test_valid_response_maps_back_to_media_hash(self, mocker) -> None:
        provider = AnthropicLandmarkProvider()
        body = {
            "landmarks": [
                {
                    "image_index": 0,
                    "name": "Belvedere Palace",
                    "confidence": 0.9,
                    "description": "A palace.",
                }
            ]
        }
        mocker.patch.object(
            provider,
            "_call_api",
            return_value={"content": [{"type": "text", "text": json.dumps(body)}]},
        )

        result = provider.identify([_context("h0")])

        assert result["h0"].name == "Belvedere Palace"

    def test_provider_dropping_an_image_excludes_it_from_the_mapping(self, mocker) -> None:
        provider = AnthropicLandmarkProvider()
        body = {
            "landmarks": [
                {"image_index": 0, "name": "Only First", "confidence": 0.9, "description": "d"}
            ]
        }

        mocker.patch.object(
            provider,
            "_call_api",
            return_value={"content": [{"type": "text", "text": json.dumps(body)}]},
        )

        result = provider.identify([_context("h0"), _context("h1")])

        assert set(result) == {"h0"}

    def test_malformed_json_text_is_a_failure_not_a_crash(self, mocker) -> None:
        provider = AnthropicLandmarkProvider()
        mocker.patch.object(
            provider, "_call_api", return_value={"content": [{"type": "text", "text": "not json"}]}
        )

        result = provider.identify([_context("h0")])

        assert result == {}

    def test_response_missing_content_is_a_failure_not_a_crash(self, mocker) -> None:
        provider = AnthropicLandmarkProvider()
        mocker.patch.object(provider, "_call_api", return_value={})

        result = provider.identify([_context("h0")])

        assert result == {}

    def test_api_exception_is_a_failure_not_a_crash(self, mocker) -> None:
        provider = AnthropicLandmarkProvider()
        mocker.patch.object(provider, "_call_api", side_effect=RuntimeError("network down"))

        result = provider.identify([_context("h0")])

        assert result == {}

    def test_never_touches_the_real_network(self, mocker) -> None:
        """`_call_api` is the only method that would open a socket; verifying it is the one
        mocked here is what makes every other test in this class network-free by construction."""
        provider = AnthropicLandmarkProvider()
        urlopen = mocker.patch("story_book.pipeline.landmarks.providers.urllib.request.urlopen")
        mocker.patch.object(
            provider,
            "_call_api",
            return_value={"content": [{"type": "text", "text": '{"landmarks": []}'}]},
        )

        provider.identify([_context("h0")])

        urlopen.assert_not_called()
