"""Bounded local image estimates; never fetch remote images to count tokens."""

import base64
import io
import math

from PIL import Image


def _dimensions(image: dict) -> tuple[int, int] | None:
    """Read image headers only, without decoding pixels or downloading URLs."""
    try:
        if image.get("path"):
            with Image.open(image["path"]) as opened:
                return opened.size
        value = image.get("image_url") or image.get("data") or ""
        if isinstance(value, dict):
            value = value.get("url", "")
        if isinstance(value, str):
            if value.startswith("data:") and ";base64," in value:
                value = value.split(",", 1)[1]
            elif "data" not in image:
                return None
            # Header inspection needs only a small prefix, even for huge images.
            value = base64.b64decode(value[:262_144], validate=True)
        if isinstance(value, bytes):
            with Image.open(io.BytesIO(value[:196_608])) as opened:
                return opened.size
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        pass
    return None


def estimate_image_tokens(image: dict, *, llm_client) -> int:
    """Respect known tile models and use a dimension-aware heuristic otherwise."""
    model = str(getattr(llm_client, "model_name", "")).lower()
    image_url = image.get("image_url")
    detail = image.get("detail") or (image_url.get("detail") if isinstance(image_url, dict) else "auto")
    tile_model = model.startswith("gpt-4o") or (
        model.startswith("gpt-4.1") and not model.startswith(("gpt-4.1-mini", "gpt-4.1-nano"))
    )
    base, tile = (2833, 5667) if model.startswith("gpt-4o-mini") else (85, 170)
    if tile_model and detail == "low":
        return base
    dimensions = _dimensions(image)
    if not dimensions:
        return base + 16 * tile if tile_model else 4096
    width, height = dimensions
    if tile_model:
        scale = min(1, 2048 / max(width, height), 768 / min(width, height))
        scaled_width = max(1, math.floor(width * scale))
        scaled_height = max(1, math.floor(height * scale))
        tiles = math.ceil(scaled_width / 512) * math.ceil(scaled_height / 512)
        return base + tile * tiles
    # Unknown/current model preprocessing differs. Preserve the size signal,
    # reserve extra room, and settle against provider-reported usage afterwards.
    if model.startswith("gpt-4.1-nano"):
        multiplier = 2.46
    elif model.startswith("gpt-4.1-mini"):
        multiplier = 1.62
    else:
        multiplier = 1.5
    return math.ceil(min(32_768, math.ceil(width / 32) * math.ceil(height / 32)) * multiplier)
