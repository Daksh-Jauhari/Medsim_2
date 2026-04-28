from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Mapping

from backend.config import get_settings


logger = logging.getLogger(__name__)


CATEGORIES = [
    "building_exterior",
    "lobby_main_entrance",
    "ed_entrance_ambulance_bay",
    "corridor_hallway",
    "nursing_station",
    "patient_room",
    "medication_room_pharmacy",
    "icu_bay",
    "operating_room",
    "utility_support",
    "other",
]


PLACES_CATEGORY_SEQUENCE = [
    "lobby_main_entrance",
    "corridor_hallway",
    "patient_room",
    "nursing_station",
    "medication_room_pharmacy",
    "icu_bay",
]


_CATEGORY_DESCRIPTIONS = "\n".join(f"- {c}" for c in CATEGORIES)
_CLAUDE_PROMPT = (
    "You are classifying a single hospital photograph for a safety-audit pipeline. "
    "Pick exactly one category from this list that best matches what the image shows:\n"
    f"{_CATEGORY_DESCRIPTIONS}\n\n"
    "Respond with ONLY a compact JSON object on one line, no prose, no markdown fences:\n"
    '{"category": "<one of the categories above>", "confidence": <float 0-1>, "notes": "<short why, <=120 chars>"}'
)


def _category_from_metadata(source: str, metadata: Mapping[str, object] | None = None) -> tuple[str, float]:
    metadata = metadata or {}
    if source == "street_view":
        heading = int(metadata.get("heading", 0))
        if heading in {135, 180, 225}:
            return "ed_entrance_ambulance_bay", 0.74
        return "building_exterior", 0.8

    if source == "places":
        index = int(metadata.get("index", 1))
        category = PLACES_CATEGORY_SEQUENCE[(index - 1) % len(PLACES_CATEGORY_SEQUENCE)]
        return category, 0.71

    if source == "supplemental_upload":
        return "patient_room", 0.69

    return "other", 0.6


def _heuristic_classification(image_bytes: bytes, source: str, metadata: Mapping[str, object] | None) -> dict:
    category, confidence = _category_from_metadata(source, metadata)
    if category == "other":
        fingerprint = sum(image_bytes) % len(CATEGORIES) if image_bytes else 0
        category = CATEGORIES[fingerprint]
        confidence = round(0.65 + (fingerprint / max(len(CATEGORIES) - 1, 1)) * 0.2, 2)
    return {
        "category": category,
        "confidence": min(confidence, 0.98),
        "notes": f"Heuristic classification for {source}",
        "source": source,
    }


def _guess_media_type(image_bytes: bytes, metadata: Mapping[str, object] | None) -> str:
    metadata = metadata or {}
    declared = str(metadata.get("content_type") or metadata.get("filetype") or "").lower()
    if declared in {"image/jpeg", "image/jpg"}:
        return "image/jpeg"
    if declared in {"image/png", "image/webp", "image/gif"}:
        return declared
    # Sniff magic bytes as a fallback
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n"):
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _parse_claude_response(text: str) -> dict | None:
    """Extract the first JSON object from Claude's response, tolerating stray prose or fences."""
    if not text:
        return None
    cleaned = re.sub(r"```[a-zA-Z]*\n?|```", "", text).strip()
    match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    category = str(obj.get("category", "")).strip()
    if category not in CATEGORIES:
        return None
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 0.99))
    notes = str(obj.get("notes", "")).strip() or "Claude vision classification"
    return {"category": category, "confidence": confidence, "notes": notes[:200]}


async def _classify_with_claude(
    image_bytes: bytes, source: str, metadata: Mapping[str, object] | None
) -> dict | None:
    """
    Call Claude Vision to classify a hospital photo.
    Returns None on any failure so callers can fall back to the heuristic.
    """
    settings = get_settings()
    if not settings.anthropic_api_key or settings.use_synthetic_fallbacks:
        return None
    try:
        import anthropic  # local import so the module doesn't hard-require the package
    except ImportError:
        logger.warning("anthropic package not installed; using heuristic classifier")
        return None

    media_type = _guess_media_type(image_bytes, metadata)
    b64 = base64.b64encode(image_bytes).decode("ascii")

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                        {"type": "text", "text": _CLAUDE_PROMPT},
                    ],
                }
            ],
        )
    except Exception as exc:  # network, auth, rate limit, malformed image, etc.
        logger.warning("Claude vision call failed (%s): %s", source, exc)
        return None

    # Extract text from the first text block in the response
    text_parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text_parts.append(getattr(block, "text", "") or "")
    parsed = _parse_claude_response("".join(text_parts))
    if parsed is None:
        logger.warning("Claude vision returned unparsable output for %s", source)
        return None
    parsed["source"] = source
    return parsed


async def classify_image(
    image_bytes: bytes, source: str, metadata: Mapping[str, object] | None = None
) -> dict:
    """
    Classify a hospital image into one of the CATEGORIES.

    Order of preference:
      1. Claude Vision (Anthropic API), when ANTHROPIC_API_KEY is set and
         MEDSENTINEL_USE_SYNTHETIC_FALLBACKS is false.
      2. Deterministic heuristic from source + metadata (used in synthetic
         mode, when no key is configured, or when the API call fails).
    """
    claude_result = await _classify_with_claude(image_bytes, source, metadata)
    if claude_result is not None:
        return claude_result
    return _heuristic_classification(image_bytes, source, metadata)
