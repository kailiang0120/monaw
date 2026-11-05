"""Screenshot coordinate proposals are single-use and bound to fresh viewport evidence."""
from __future__ import annotations

from dataclasses import dataclass
import time

from .session import ToolError


@dataclass
class VisualTarget:
    token: str
    page_id: int
    state: dict
    created_at: float
    consumed: bool = False

    def validate(self, page_id, current, x, y):
        if self.consumed or time.monotonic() - self.created_at > 30:
            raise ToolError('unsafe_visual_fallback: screenshot expired or was already used. Capture a fresh viewport.')
        if page_id != self.page_id or not current or current != self.state:
            raise ToolError('unsafe_visual_fallback: document, content, scroll, viewport or DPI changed since capture.')
        if not (0 <= x < current['width'] and 0 <= y < current['height']):
            raise ToolError('unsafe_visual_fallback: coordinates must be inside the captured viewport.')
