"""Unit tests for `pipeline/embeddings.py`.

No DB, filesystem, or network -- the CLIP model itself is always mocked. Real-model
coverage lives in `tests/backend/test_embeddings.py`.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from story_book.config import Config
from story_book.db.models import MediaKind
from story_book.pipeline import embeddings
from story_book.pipeline.base import StageContext
from story_book.pipeline.embeddings import (
    CLIP_MODEL_TAG,
    EmbeddingStage,
    clip_importable,
    decode_vector,
    encode_vector,
)


def _make_ctx(tmp_path: Path, config: Config | None = None) -> StageContext:
    return StageContext(
        conn=None, config=config or Config(), out_dir=tmp_path, source_dir=tmp_path / "src"
    )


class TestEncodeVector:
    def test_packs_as_little_endian_float32(self):
        blob = encode_vector([1.0, -2.5, 0.0])
        assert blob == struct.pack("<3f", 1.0, -2.5, 0.0)

    def test_length_is_four_bytes_per_value(self):
        blob = encode_vector([0.1] * 512)
        assert len(blob) == 512 * 4

    def test_empty_vector_encodes_to_empty_bytes(self):
        assert encode_vector([]) == b""


class TestDecodeVector:
    def test_empty_bytes_decode_to_empty_list(self):
        assert decode_vector(b"") == []

    def test_inverts_encode_for_typical_values(self):
        values = [0.123456, -0.987654, 1.0, -1.0, 0.0]
        assert decode_vector(encode_vector(values)) == pytest.approx(values, abs=1e-6)

    def test_inverts_encode_for_many_dimensions(self):
        values = [(i - 256) / 512.0 for i in range(512)]
        assert decode_vector(encode_vector(values)) == pytest.approx(values, abs=1e-6)

    def test_round_trip_preserves_order(self):
        values = [3.0, 1.0, 2.0]
        assert decode_vector(encode_vector(values)) == pytest.approx(values)

    def test_raises_on_truncated_blob(self):
        with pytest.raises(struct.error):
            decode_vector(b"\x00\x00\x00")


class TestClipModelTag:
    def test_is_name_slash_pretrained(self):
        assert CLIP_MODEL_TAG == "ViT-B-32/laion2b_s34b_b79k"


class TestClipImportable:
    def test_true_when_both_specs_found(self, mocker):
        mocker.patch("importlib.util.find_spec", return_value=object())
        available, reason = clip_importable()
        assert available is True
        assert reason == ""

    def test_false_when_torch_missing(self, mocker):
        def fake_find_spec(name):
            return None if name == "torch" else object()

        mocker.patch("importlib.util.find_spec", side_effect=fake_find_spec)
        available, reason = clip_importable()
        assert available is False
        assert "torch" in reason

    def test_false_when_open_clip_missing(self, mocker):
        def fake_find_spec(name):
            return None if name == "open_clip" else object()

        mocker.patch("importlib.util.find_spec", side_effect=fake_find_spec)
        available, reason = clip_importable()
        assert available is False
        assert "open_clip" in reason


class TestEmbeddingStageAvailable:
    def test_delegates_to_clip_importable(self, mocker, tmp_path):
        mocker.patch("story_book.pipeline.embeddings.clip_importable", return_value=(False, "nope"))
        stage = EmbeddingStage()
        assert stage.available(_make_ctx(tmp_path)) == (False, "nope")


class TestEmbeddingStageSelect:
    def test_picks_up_batch_size_from_config(self, mocker, tmp_path):
        config = Config()
        mocker.patch("story_book.pipeline.embeddings.db.iter_media", return_value=iter([]))
        mocker.patch("story_book.pipeline.embeddings._cached_hashes", return_value=set())
        stage = EmbeddingStage()
        stage.select(_make_ctx(tmp_path, config))
        assert stage.batch_size == config.models.clip_batch_size

    def test_only_offers_images(self, mocker, tmp_path, make_media):
        image = make_media("img", kind=MediaKind.IMAGE)
        mocker.patch("story_book.pipeline.embeddings.db.iter_media", return_value=iter([image]))
        mocker.patch("story_book.pipeline.embeddings._cached_hashes", return_value=set())
        stage = EmbeddingStage()
        result = stage.select(_make_ctx(tmp_path))
        assert result == [image]

    def test_asks_iter_media_for_images_only(self, mocker, tmp_path):
        iter_media = mocker.patch(
            "story_book.pipeline.embeddings.db.iter_media", return_value=iter([])
        )
        mocker.patch("story_book.pipeline.embeddings._cached_hashes", return_value=set())
        stage = EmbeddingStage()
        stage.select(_make_ctx(tmp_path))
        _, kwargs = iter_media.call_args
        assert kwargs["kind"] == str(MediaKind.IMAGE)

    def test_filters_out_media_already_cached_for_this_model_tag(
        self, mocker, tmp_path, make_media
    ):
        cached_media = make_media("cached")
        fresh_media = make_media("fresh")
        mocker.patch(
            "story_book.pipeline.embeddings.db.iter_media",
            return_value=iter([cached_media, fresh_media]),
        )
        mocker.patch("story_book.pipeline.embeddings._cached_hashes", return_value={"cached"})
        stage = EmbeddingStage()
        result = stage.select(_make_ctx(tmp_path))
        assert result == [fresh_media]


class TestEmbeddingStageProcessBatch:
    def test_stores_and_reports_a_result_per_item(self, mocker, tmp_path, make_media):
        media_a = make_media("a")
        media_b = make_media("b")
        fake_runner = mocker.Mock(model_tag="ViT-B-32/laion2b_s34b_b79k", dim=3)
        fake_runner.embed_images.return_value = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        mocker.patch("story_book.pipeline.embeddings.load_clip", return_value=fake_runner)
        store = mocker.patch("story_book.pipeline.embeddings._store_embedding")

        stage = EmbeddingStage()
        results = stage.process_batch(_make_ctx(tmp_path), [media_a, media_b])

        assert set(results) == {"a", "b"}
        assert store.call_count == 2

    def test_passes_media_paths_to_embed_images(self, mocker, tmp_path, make_media):
        media = make_media("a", path="/src/a.jpg")
        fake_runner = mocker.Mock(model_tag="tag", dim=2)
        fake_runner.embed_images.return_value = [[0.1, 0.2]]
        mocker.patch("story_book.pipeline.embeddings.load_clip", return_value=fake_runner)
        mocker.patch("story_book.pipeline.embeddings._store_embedding")

        stage = EmbeddingStage()
        stage.process_batch(_make_ctx(tmp_path), [media])

        (paths,), _ = fake_runner.embed_images.call_args
        assert [str(p) for p in paths] == ["/src/a.jpg"]

    def test_never_loads_clip_when_batch_is_empty(self, mocker, tmp_path):
        load_clip = mocker.patch("story_book.pipeline.embeddings.load_clip")
        fake_runner = mocker.Mock(model_tag="tag", dim=2)
        fake_runner.embed_images.return_value = []
        load_clip.return_value = fake_runner

        stage = EmbeddingStage()
        results = stage.process_batch(_make_ctx(tmp_path), [])

        assert results == {}


class TestLoadClipCaching:
    def test_returns_cached_runner_for_same_config(self, mocker):
        embeddings._clip_cache.clear()
        fake_runner = mocker.Mock()
        create_runner = mocker.patch(
            "story_book.pipeline.embeddings.ClipRunner", return_value=fake_runner
        )
        mocker.patch("story_book.pipeline.embeddings._resolve_device", return_value="cpu")
        fake_open_clip = mocker.Mock()
        fake_open_clip.create_model_and_transforms.return_value = (
            mocker.Mock(),
            None,
            mocker.Mock(),
        )
        fake_open_clip.get_tokenizer.return_value = mocker.Mock()
        mocker.patch.dict("sys.modules", {"open_clip": fake_open_clip})

        config = Config()
        first = embeddings.load_clip(config)
        second = embeddings.load_clip(config)

        assert first is second is fake_runner
        assert create_runner.call_count == 1
        embeddings._clip_cache.clear()

    def test_different_device_gets_its_own_runner(self, mocker):
        embeddings._clip_cache.clear()
        mocker.patch(
            "story_book.pipeline.embeddings.ClipRunner",
            side_effect=lambda **kwargs: mocker.Mock(**kwargs),
        )
        fake_open_clip = mocker.Mock()
        fake_open_clip.create_model_and_transforms.return_value = (
            mocker.Mock(),
            None,
            mocker.Mock(),
        )
        fake_open_clip.get_tokenizer.return_value = mocker.Mock()
        mocker.patch.dict("sys.modules", {"open_clip": fake_open_clip})

        devices = iter(["cpu", "mps"])
        mocker.patch(
            "story_book.pipeline.embeddings._resolve_device", side_effect=lambda _: next(devices)
        )

        config = Config()
        first = embeddings.load_clip(config)
        second = embeddings.load_clip(config)

        assert first is not second
        embeddings._clip_cache.clear()
