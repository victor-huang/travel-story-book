"""Concrete landmark providers.

Two vendor implementations prove the `LandmarkProvider` seam actually seams: swap
`config.landmarks.provider` and nothing else in the pipeline changes.

Neither the `anthropic` SDK nor `requests` is a declared dependency of this project (see
`pyproject.toml`, which this task does not own), so both providers call their vision API via
`urllib.request` directly -- exactly the raw-HTTP fallback the claude-api skill itself
prescribes when no SDK is available. Every network call is isolated in a single `_call_api`
method so tests can `mocker.patch` it; nothing in this module ever reaches the network in a
test.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.request
from typing import Any

from story_book.config import LandmarkConfig

from .base import CostEstimate, LandmarkIdentification, LandmarkImageContext, LandmarkProvider

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

_DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
_DEFAULT_OPENAI_MODEL = "gpt-4o"

# Pricing basis for the Anthropic provider, per the claude-api skill's cached "Current Models"
# table (cache date 2026-06-24): Claude Opus 5 (`claude-opus-5`) is $5.00 / 1M input tokens and
# $25.00 / 1M output tokens. This is the model the skill instructs to default to unless the
# user names another, and it supports vision + `output_config.format` structured output.
_ANTHROPIC_PRICE_PER_MTOK_INPUT = 5.00
_ANTHROPIC_PRICE_PER_MTOK_OUTPUT = 25.00

# Per-image and per-request token estimates used only for the pre-call cost estimate. These
# are budgeting approximations (a typical trip photo at standard, non-high-res encoding plus a
# short structured-JSON answer), not a guarantee -- the "estimate is within 20% of actual" half
# of the T25 acceptance criterion needs a real key and real photos to verify; see the provider
# interface's docstring and this task's final summary for what remains unverified.
_ANTHROPIC_TOKENS_PER_IMAGE_INPUT = 1_600
_ANTHROPIC_TOKENS_PER_IMAGE_OUTPUT = 150
_ANTHROPIC_PROMPT_OVERHEAD_TOKENS = 300

# The second provider exists to prove the interface generalizes across vendors, per the T25
# spec's "at least two implementations" requirement. OpenAI is out of scope for the claude-api
# skill this task was required to consult, so these pricing constants are illustrative only --
# do not budget a real run against this provider without checking current OpenAI pricing.
_OPENAI_PRICE_PER_MTOK_INPUT = 2.50
_OPENAI_PRICE_PER_MTOK_OUTPUT = 10.00
_OPENAI_TOKENS_PER_IMAGE_INPUT = 1_100
_OPENAI_TOKENS_PER_IMAGE_OUTPUT = 150
_OPENAI_PROMPT_OVERHEAD_TOKENS = 300


def _response_schema() -> dict[str, Any]:
    """Structured-output schema shared by both providers: one entry per submitted image."""
    return {
        "type": "object",
        "properties": {
            "landmarks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "image_index": {"type": "integer"},
                        "name": {"type": "string"},
                        "confidence": {"type": "number"},
                        "description": {"type": "string"},
                        "notable_feature": {"type": ["string", "null"]},
                    },
                    "required": ["image_index", "name", "confidence", "description"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["landmarks"],
        "additionalProperties": False,
    }


def _validate_entries(raw: Any) -> dict[int, LandmarkIdentification]:
    """Validate a parsed JSON body into `{image_index: identification}`.

    Anything that doesn't match the expected shape -- wrong top-level type, a missing field, a
    confidence outside [0, 1] -- is dropped rather than raised. A malformed entry is a failure
    for that one image, never a crash for the batch.
    """
    out: dict[int, LandmarkIdentification] = {}
    if not isinstance(raw, dict):
        return out
    entries = raw.get("landmarks")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("image_index")
        name = entry.get("name")
        confidence = entry.get("confidence")
        description = entry.get("description")
        feature = entry.get("notable_feature")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            continue
        if not (0.0 <= float(confidence) <= 1.0):
            continue
        if not isinstance(description, str):
            continue
        if feature is not None and not isinstance(feature, str):
            continue
        out[index] = LandmarkIdentification(
            name=name.strip(),
            confidence=float(confidence),
            description=description.strip(),
            notable_feature=feature.strip()
            if isinstance(feature, str) and feature.strip()
            else None,
        )
    return out


def _by_media_hash(
    batch: list[LandmarkImageContext], by_index: dict[int, LandmarkIdentification]
) -> dict[str, LandmarkIdentification]:
    return {
        batch[index].media.hash: identification
        for index, identification in by_index.items()
        if 0 <= index < len(batch)
    }


def _context_prompt(index: int, context: LandmarkImageContext) -> str:
    parts = [f"Image {index}:"]
    media = context.media
    if media.lat is not None and media.lon is not None:
        parts.append(f"coordinates ({media.lat:.5f}, {media.lon:.5f})")
    if context.place_label:
        parts.append(f"near {context.place_label}")
    return " ".join(parts)


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


class AnthropicLandmarkProvider(LandmarkProvider):
    """Primary provider: Claude's vision API via the Messages endpoint.

    Sends every image in the request plus its coordinates and reverse-geocoded place name as
    text context -- per the plan, that context is the difference between "a palace" and a
    specific named landmark. Requests structured JSON output via `output_config.format` (no
    beta header required for that parameter, per the claude-api skill).
    """

    name = "anthropic"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or _DEFAULT_ANTHROPIC_MODEL

    def available(self) -> tuple[bool, str]:
        if not os.environ.get(ANTHROPIC_API_KEY_ENV):
            return False, f"{ANTHROPIC_API_KEY_ENV} is not set"
        return True, ""

    def estimate_cost(self, image_count: int, images_per_request: int) -> CostEstimate:
        if image_count <= 0 or images_per_request <= 0:
            return CostEstimate(request_count=0, estimated_usd=0.0, model=self.model)
        request_count = _ceil_div(image_count, images_per_request)
        input_tokens = request_count * (
            _ANTHROPIC_PROMPT_OVERHEAD_TOKENS
            + images_per_request * _ANTHROPIC_TOKENS_PER_IMAGE_INPUT
        )
        output_tokens = request_count * images_per_request * _ANTHROPIC_TOKENS_PER_IMAGE_OUTPUT
        cost = (
            input_tokens / 1_000_000 * _ANTHROPIC_PRICE_PER_MTOK_INPUT
            + output_tokens / 1_000_000 * _ANTHROPIC_PRICE_PER_MTOK_OUTPUT
        )
        return CostEstimate(
            request_count=request_count, estimated_usd=round(cost, 4), model=self.model
        )

    def identify(self, batch: list[LandmarkImageContext]) -> dict[str, LandmarkIdentification]:
        content: list[dict[str, Any]] = []
        for index, context in enumerate(batch):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": context.media_type,
                        "data": base64.standard_b64encode(context.image_bytes).decode("ascii"),
                    },
                }
            )
            content.append({"type": "text", "text": _context_prompt(index, context)})
        content.append(
            {
                "type": "text",
                "text": (
                    "Identify the landmark shown in each image above, using its coordinates "
                    "and nearby place name as context. Respond for every image, even if "
                    "uncertain -- use a low confidence value instead of omitting an entry."
                ),
            }
        )
        payload = {
            "model": self.model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": content}],
            "output_config": {"format": {"type": "json_schema", "schema": _response_schema()}},
        }
        try:
            response = self._call_api(payload)
        except Exception:
            logger.exception("landmarks: anthropic request failed")
            return {}
        text = _anthropic_text(response)
        if text is None:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("landmarks: anthropic returned non-JSON structured output")
            return {}
        return _by_media_hash(batch, _validate_entries(parsed))

    def _call_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The one place this class touches the network. Always mocked in tests."""
        api_key = os.environ[ANTHROPIC_API_KEY_ENV]
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))


def _anthropic_text(response: dict[str, Any]) -> str | None:
    content = response.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            return block["text"]
    return None


class OpenAILandmarkProvider(LandmarkProvider):
    """Second vendor implementation, proving the `LandmarkProvider` seam generalizes.

    Pricing constants are illustrative only -- OpenAI pricing is outside the scope of the
    claude-api skill this task was required to consult before writing any provider code.
    """

    name = "openai"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or _DEFAULT_OPENAI_MODEL

    def available(self) -> tuple[bool, str]:
        if not os.environ.get(OPENAI_API_KEY_ENV):
            return False, f"{OPENAI_API_KEY_ENV} is not set"
        return True, ""

    def estimate_cost(self, image_count: int, images_per_request: int) -> CostEstimate:
        if image_count <= 0 or images_per_request <= 0:
            return CostEstimate(request_count=0, estimated_usd=0.0, model=self.model)
        request_count = _ceil_div(image_count, images_per_request)
        input_tokens = request_count * (
            _OPENAI_PROMPT_OVERHEAD_TOKENS + images_per_request * _OPENAI_TOKENS_PER_IMAGE_INPUT
        )
        output_tokens = request_count * images_per_request * _OPENAI_TOKENS_PER_IMAGE_OUTPUT
        cost = (
            input_tokens / 1_000_000 * _OPENAI_PRICE_PER_MTOK_INPUT
            + output_tokens / 1_000_000 * _OPENAI_PRICE_PER_MTOK_OUTPUT
        )
        return CostEstimate(
            request_count=request_count, estimated_usd=round(cost, 4), model=self.model
        )

    def identify(self, batch: list[LandmarkImageContext]) -> dict[str, LandmarkIdentification]:
        content: list[dict[str, Any]] = []
        for index, context in enumerate(batch):
            content.append({"type": "text", "text": _context_prompt(index, context)})
            encoded = base64.standard_b64encode(context.image_bytes).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{context.media_type};base64,{encoded}"},
                }
            )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "landmarks", "schema": _response_schema()},
            },
        }
        try:
            response = self._call_api(payload)
        except Exception:
            logger.exception("landmarks: openai request failed")
            return {}
        text = _openai_text(response)
        if text is None:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("landmarks: openai returned non-JSON structured output")
            return {}
        return _by_media_hash(batch, _validate_entries(parsed))

    def _call_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The one place this class touches the network. Always mocked in tests."""
        api_key = os.environ[OPENAI_API_KEY_ENV]
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))


def _openai_text(response: dict[str, Any]) -> str | None:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        return None
    text = message.get("content")
    return text if isinstance(text, str) else None


_REGISTRY: dict[str, type[LandmarkProvider]] = {
    AnthropicLandmarkProvider.name: AnthropicLandmarkProvider,
    OpenAILandmarkProvider.name: OpenAILandmarkProvider,
}


def get_provider(config: LandmarkConfig) -> LandmarkProvider | None:
    """Build the configured provider, or `None` for `provider = "none"` / an unknown name."""
    provider_cls = _REGISTRY.get(config.provider)
    if provider_cls is None:
        return None
    return provider_cls(model=config.model)
