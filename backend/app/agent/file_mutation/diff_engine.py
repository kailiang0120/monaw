"""Produce bounded, display-safe unified diffs for file mutation previews."""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass

MAX_DIFF_INPUT_BYTES = 256 * 1024
MAX_DIFF_INPUT_LINES = 5_000
MAX_DIFF_OUTPUT_BYTES = 48 * 1024
MAX_DIFF_OUTPUT_LINES = 800

_SUPPORTED_ENCODINGS = frozenset(
    {
        "utf-8",
        "utf-8-sig",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "ascii",
        "cp1252",
        "windows-1252",
        "latin-1",
        "iso-8859-1",
        "shift_jis",
        "shift-jis",
        "gbk",
        "gb18030",
        "big5",
    }
)


@dataclass(frozen=True, slots=True)
class DiffPreview:
    """A public diff or content-free summary safe to include in a preview."""

    kind: str
    text: str = ""
    reason: str = ""
    truncated: bool = False
    before_bytes: int | None = None
    after_bytes: int | None = None
    before_lines: int | None = None
    after_lines: int | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind}
        if self.text:
            result["text"] = self.text
        if self.reason:
            result["reason"] = self.reason
        if self.truncated:
            result["truncated"] = True
        for key in ("before_bytes", "after_bytes", "before_lines", "after_lines"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


def _safe_header(value: str) -> str:
    """Keep paths from injecting extra diff headers or terminal controls."""
    return "".join(
        char if char not in "\r\n" and unicodedata.category(char) not in {"Cc", "Cf"} else " "
        for char in str(value)
    )


def _safe_diff_line(value: str) -> str:
    """Render control and format characters visibly so contents cannot spoof a diff."""
    safe: list[str] = []
    for char in value:
        if char in "\n\t":
            safe.append(char)
        elif unicodedata.category(char) in {"Cc", "Cf"}:
            codepoint = ord(char)
            if codepoint <= 0xFF:
                safe.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                safe.append(f"\\u{codepoint:04x}")
            else:
                safe.append(f"\\U{codepoint:08x}")
        else:
            safe.append(char)
    return "".join(safe)


def _decode(data: bytes, encoding: str) -> str | None:
    if not data:
        return ""
    requested = (encoding or "utf-8").strip().lower().replace("_", "-")
    candidates: list[str] = []
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    elif data.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    if requested in _SUPPORTED_ENCODINGS:
        candidates.append(requested)
    candidates.append("utf-8-sig")

    # NUL bytes usually identify binary input. Permit them only when an
    # explicit UTF-16 encoding or BOM can decode them as ordinary text.
    utf16_requested = requested in {"utf-16", "utf-16-le", "utf-16-be"}
    has_utf16_bom = data.startswith((b"\xff\xfe", b"\xfe\xff"))
    if b"\x00" in data and not (utf16_requested or has_utf16_bom):
        return None

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            decoded = data.decode(candidate, errors="strict")
            if "\x00" in decoded:
                return None
            return decoded
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _summary(
    reason: str,
    before: bytes | None,
    after: bytes | None,
    *,
    before_lines: int | None = None,
    after_lines: int | None = None,
) -> DiffPreview:
    return DiffPreview(
        kind="summary",
        reason=reason,
        before_bytes=len(before) if before is not None else None,
        after_bytes=len(after) if after is not None else None,
        before_lines=before_lines,
        after_lines=after_lines,
    )


def unified_diff_preview(
    before: bytes | None,
    after: bytes | None,
    *,
    fromfile: str = "before",
    tofile: str = "after",
    encoding: str = "utf-8",
    max_input_bytes: int = MAX_DIFF_INPUT_BYTES,
    max_input_lines: int = MAX_DIFF_INPUT_LINES,
    max_output_bytes: int = MAX_DIFF_OUTPUT_BYTES,
    max_output_lines: int = MAX_DIFF_OUTPUT_LINES,
) -> DiffPreview:
    """Return a bounded text diff or a content-free reason summary.

    ``None`` means that the corresponding path is absent. Callers must pass
    actual bytes only after the relevant read permission has been checked.
    Input and output limits are independently enforced before returning data.
    """
    old_bytes = before or b""
    new_bytes = after or b""
    if len(old_bytes) + len(new_bytes) > max_input_bytes:
        return _summary("input_limit", before, after)

    old_text = _decode(old_bytes, encoding)
    new_text = _decode(new_bytes, encoding)
    if old_text is None or new_text is None:
        return _summary("binary_or_undecodable", before, after)

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    if len(old_lines) > max_input_lines or len(new_lines) > max_input_lines:
        return _summary(
            "line_limit",
            before,
            after,
            before_lines=len(old_lines),
            after_lines=len(new_lines),
        )

    raw_diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=_safe_header(fromfile),
        tofile=_safe_header(tofile),
        lineterm="\n",
    )
    output: list[str] = []
    output_bytes = 0
    output_lines = 0
    truncated = False
    for line in raw_diff:
        # difflib leaves a final source line unterminated when the file has
        # no final newline. Keep that distinction visible and valid in output.
        if line and not line.endswith(("\n", "\r")):
            line = f"{line}\n\\ No newline at end of file\n"
        safe_line = _safe_diff_line(line)
        encoded_size = len(safe_line.encode("utf-8"))
        line_count = safe_line.count("\n") or 1
        if output_lines + line_count > max_output_lines or output_bytes + encoded_size > max_output_bytes:
            truncated = True
            break
        output.append(safe_line)
        output_bytes += encoded_size
        output_lines += line_count

    text = "".join(output)
    marker = "[diff truncated by preview limit]\n"
    if (
        truncated
        and output_lines < max_output_lines
        and len(text.encode("utf-8")) + len(marker.encode("utf-8")) <= max_output_bytes
    ):
        text += marker
    return DiffPreview(
        kind="unified_diff",
        text=text,
        truncated=truncated,
        before_bytes=len(old_bytes),
        after_bytes=len(new_bytes),
        before_lines=len(old_lines),
        after_lines=len(new_lines),
    )
