from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from story_book.config import Config, ConfigError, DeviceConfig

EXAMPLE_CONFIG = Path(__file__).parents[2] / "config.example.toml"


class TestConfigDefaults:
    def test_defaults_construct_without_arguments(self) -> None:
        assert Config().time.day_start_hour == 4

    def test_no_home_location_by_default(self) -> None:
        assert Config().home is None

    def test_cloud_is_enabled_by_default(self) -> None:
        assert Config().no_cloud is False

    def test_landmark_provider_defaults_to_none(self) -> None:
        assert Config().landmarks.provider == "none"

    def test_load_without_path_returns_defaults(self) -> None:
        assert Config.load(None) == Config()


class TestConfigFromDict:
    def test_nested_table_overrides_single_value(self) -> None:
        config = Config.from_dict({"events": {"gap_minutes": 45.0}})
        assert config.events.gap_minutes == 45.0

    def test_nested_table_keeps_unspecified_defaults(self) -> None:
        config = Config.from_dict({"events": {"gap_minutes": 45.0}})
        assert config.events.jump_km == Config().events.jump_km

    def test_home_location_is_parsed(self) -> None:
        config = Config.from_dict({"home": {"lat": 37.77, "lon": -122.41, "exclusion_km": 8.0}})
        assert config.home is not None and config.home.exclusion_km == 8.0

    def test_deeply_nested_weights_are_parsed(self) -> None:
        config = Config.from_dict({"quality": {"weights": {"sharpness": 0.9}}})
        assert config.quality.weights.sharpness == 0.9

    def test_list_becomes_tuple_for_frozen_dataclass(self) -> None:
        config = Config.from_dict({"quality": {"reject_content_classes": ["receipt"]}})
        assert config.quality.reject_content_classes == ("receipt",)

    def test_devices_are_keyed_by_model_string(self) -> None:
        config = Config.from_dict({"devices": {"ILCE-7M4": {"clock_offset_minutes": -60}}})
        assert config.devices["ILCE-7M4"] == DeviceConfig(clock_offset_minutes=-60)


class TestConfigValidation:
    def test_unknown_top_level_key_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown key"):
            Config.from_dict({"nonsense": 1})

    def test_unknown_nested_key_names_its_table(self) -> None:
        with pytest.raises(ConfigError, match="events"):
            Config.from_dict({"events": {"gap_seconds": 1}})

    def test_unsupported_config_version_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="config_version"):
            Config.from_dict({"config_version": 99})

    def test_invalid_transcribe_mode_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="transcribe"):
            Config.from_dict({"video": {"transcribe": "sometimes"}})

    def test_missing_file_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            Config.load(tmp_path / "absent.toml")


class TestExampleConfig:
    def test_example_config_is_valid(self) -> None:
        with EXAMPLE_CONFIG.open("rb") as handle:
            raw = tomllib.load(handle)
        assert Config.from_dict(raw) is not None

    def test_example_config_matches_code_defaults(self) -> None:
        """Every uncommented value in the example must be the real default, or the docs lie."""
        with EXAMPLE_CONFIG.open("rb") as handle:
            raw = tomllib.load(handle)
        assert Config.from_dict(raw) == Config()
