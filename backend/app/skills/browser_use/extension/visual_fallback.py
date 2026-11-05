"""Fixed visual hit test; coordinate-only consequential dispatch is unsupported."""
from __future__ import annotations

VISUAL_HIT_TEST = r"""function(x,y) {
  const el = document.elementFromPoint(x,y);
  if (!el || el.closest('iframe,frame')) return null;
  const r = el.getBoundingClientRect(), style = getComputedStyle(el);
  if (!el.isConnected || r.width <= 0 || r.height <= 0 || style.visibility === 'hidden' || el.disabled || el.closest('[inert],[aria-disabled="true"]')) return null;
  return el;
}"""
