"""Prompt injection threat detection for context files.

Scans text for injection patterns before including in LLM prompts.
Returns [BLOCKED] if threats are found. Truncates to 20K chars with
head/tail split for oversized content.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_CONTEXT_CHARS = 20_000
HEAD_TAIL_CHARS = 8_000

_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
     "instruction_override"),
    (re.compile(r"forget\s+(everything|all|your)\s+(previous|prior|above)", re.I),
     "instruction_override"),
    (re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.I),
     "identity_override"),
    (re.compile(r"new\s+system\s+prompt", re.I),
     "system_prompt_override"),
    (re.compile(r"system:\s*you\s+are", re.I),
     "system_prompt_injection"),
    (re.compile(r"<\|?(system|endof|im_start)\|?>", re.I),
     "delimiter_injection"),
    (re.compile(r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>", re.I),
     "delimiter_injection"),
    (re.compile(r"(curl|wget|fetch)\s+https?://.*\|", re.I),
     "exfiltration_attempt"),
    (re.compile(r"send\s+(this|the|all|my)\s+(data|content|info|text|conversation)\s+to", re.I),
     "exfiltration_attempt"),
    (re.compile(r"base64\s*(encode|decode)", re.I),
     "encoding_evasion"),
    # Invisible Unicode characters (zero-width joiners, RTL overrides, etc.)
    (re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2028\u2029\u202a-\u202e\ufeff]"),
     "invisible_unicode"),
    (re.compile(r"do\s+not\s+(follow|obey|listen|adhere)", re.I),
     "instruction_override"),
    (re.compile(r"override\s+(your|the|all)\s+(rules|instructions|guidelines)", re.I),
     "instruction_override"),
    (re.compile(r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don.t\s+have)", re.I),
     "constraint_bypass"),
    (re.compile(r"pretend\s+(that|you)", re.I),
     "identity_override"),
    (re.compile(r"jailbreak|DAN\s*mode|developer\s+mode", re.I),
     "jailbreak_attempt"),
]


@dataclass
class InjectionThreat:
    """Details of a detected injection pattern."""
    pattern_name: str
    matched_text: str
    position: int


def _normalize(text: str) -> str:
    """H6: Normalize input to defeat simple obfuscation tricks.

    NFKC folds unicode lookalikes; control-char strip removes zero-width
    joiners and other invisible markers; whitespace collapse defeats
    multi-space padding like 'ignore  all   previous'.
    """
    # Strip control characters (keep newline/tab for context)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\t")
    # NFKC: fold ｉｇｎｏｒｅ → ignore, etc.
    text = unicodedata.normalize("NFKC", text)
    # Collapse runs of whitespace within lines (preserve newlines)
    text = re.sub(r"[^\S\n]+", " ", text)
    return text


def scan_for_injection(text: str) -> list[InjectionThreat]:
    """Scan text for prompt injection patterns.

    Returns a list of detected threats (empty if clean).
    """
    normalized = _normalize(text)
    threats: list[InjectionThreat] = []
    for pattern, name in _INJECTION_PATTERNS:
        for match in pattern.finditer(normalized):
            threats.append(InjectionThreat(
                pattern_name=name,
                matched_text=match.group()[:100],
                position=match.start(),
            ))
    return threats


def sanitize_context(text: str, label: str = "context") -> str:
    """Sanitize context text: scan for injections and truncate if needed.

    Returns sanitized text, or '[BLOCKED]' if threats detected.
    """
    threats = scan_for_injection(text)
    if threats:
        threat_names = {t.pattern_name for t in threats}
        return f"[BLOCKED: {label} contained injection patterns: {', '.join(threat_names)}]"

    # Truncate with head/tail split
    if len(text) > MAX_CONTEXT_CHARS:
        text = (
            text[:HEAD_TAIL_CHARS]
            + f"\n\n... [{label}: {len(text)} chars truncated to {MAX_CONTEXT_CHARS}] ...\n\n"
            + text[-HEAD_TAIL_CHARS:]
        )

    return text
