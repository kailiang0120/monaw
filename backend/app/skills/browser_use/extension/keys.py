"""Key names for press_key, turned into CDP Input.dispatchKeyEvent parameters."""

from __future__ import annotations

from dataclasses import dataclass

from .session import ToolError

MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
_MODIFIER_ALIASES = {
    "alt": "Alt", "option": "Alt",
    "control": "Control", "ctrl": "Control",
    "meta": "Meta", "cmd": "Meta", "command": "Meta", "win": "Meta",
    "shift": "Shift",
}


@dataclass(frozen=True)
class KeyDefinition:
    key: str
    code: str
    key_code: int
    text: str = ""


_SPECIAL: dict[str, KeyDefinition] = {
    "Enter": KeyDefinition("Enter", "Enter", 13, "\r"),
    "Tab": KeyDefinition("Tab", "Tab", 9),
    "Escape": KeyDefinition("Escape", "Escape", 27),
    "Backspace": KeyDefinition("Backspace", "Backspace", 8),
    "Delete": KeyDefinition("Delete", "Delete", 46),
    "Insert": KeyDefinition("Insert", "Insert", 45),
    "Home": KeyDefinition("Home", "Home", 36),
    "End": KeyDefinition("End", "End", 35),
    "PageUp": KeyDefinition("PageUp", "PageUp", 33),
    "PageDown": KeyDefinition("PageDown", "PageDown", 34),
    "ArrowLeft": KeyDefinition("ArrowLeft", "ArrowLeft", 37),
    "ArrowUp": KeyDefinition("ArrowUp", "ArrowUp", 38),
    "ArrowRight": KeyDefinition("ArrowRight", "ArrowRight", 39),
    "ArrowDown": KeyDefinition("ArrowDown", "ArrowDown", 40),
    "Space": KeyDefinition(" ", "Space", 32, " "),
    "Alt": KeyDefinition("Alt", "AltLeft", 18),
    "Control": KeyDefinition("Control", "ControlLeft", 17),
    "Meta": KeyDefinition("Meta", "MetaLeft", 91),
    "Shift": KeyDefinition("Shift", "ShiftLeft", 16),
    **{f"F{n}": KeyDefinition(f"F{n}", f"F{n}", 111 + n) for n in range(1, 13)},
}
_ALIASES = {
    "return": "Enter", "esc": "Escape", "del": "Delete", "up": "ArrowUp", "down": "ArrowDown",
    "left": "ArrowLeft", "right": "ArrowRight", "pgup": "PageUp", "pgdn": "PageDown", "spacebar": "Space",
}
_PUNCTUATION = {
    "-": ("Minus", 189), "=": ("Equal", 187), ",": ("Comma", 188), ".": ("Period", 190),
    "/": ("Slash", 191), ";": ("Semicolon", 186), "'": ("Quote", 222), "[": ("BracketLeft", 219),
    "]": ("BracketRight", 221), "\\": ("Backslash", 220), "`": ("Backquote", 192), "+": ("Equal", 187),
}


def key_definition(name: str) -> KeyDefinition:
    if name == " ":
        return _SPECIAL["Space"]
    lowered = name.lower()
    for candidate in (name, _ALIASES.get(lowered, "")):
        if candidate in _SPECIAL:
            return _SPECIAL[candidate]
    matched = next((key for key in _SPECIAL if key.lower() == lowered), None)
    if matched:
        return _SPECIAL[matched]
    if len(name) == 1:
        if name.isalpha() and name.isascii():
            return KeyDefinition(name, f"Key{name.upper()}", ord(name.upper()), name)
        if name.isdigit():
            return KeyDefinition(name, f"Digit{name}", ord(name), name)
        code, key_code = _PUNCTUATION.get(name, ("", 0))
        return KeyDefinition(name, code, key_code, name)
    raise ToolError(f'Unknown key "{name}". Use names like Enter, Tab, Escape, ArrowDown, a, or Control+A.')


def parse_key_combo(combo: str) -> tuple[list[str], KeyDefinition]:
    """Split "Control+Shift+R" (or "Control++") into modifiers and the main key."""
    text = combo.strip()
    if not text:
        raise ToolError("A key is required.")
    if text.endswith("++"):
        modifier_names, main = text[:-2].split("+") if len(text) > 2 else [], "+"
    elif "+" in text and text != "+":
        *modifier_names, main = text.split("+")
    else:
        modifier_names, main = [], text
    modifiers: list[str] = []
    for raw in modifier_names:
        canonical = _MODIFIER_ALIASES.get(raw.strip().lower())
        if canonical is None:
            raise ToolError(f'Unknown modifier "{raw}". Use Control, Shift, Alt or Meta.')
        if canonical not in modifiers:
            modifiers.append(canonical)
    main = main.strip() or main
    if modifiers and len(main) == 1 and main.isalpha():
        # "Control+A" means the A key, not a capital letter; Shift decides case.
        main = main.lower()
    return modifiers, key_definition(main)


def key_events(combo: str) -> list[dict]:
    """The keyDown/keyUp sequence for a key press, modifiers held around it."""
    modifiers, definition = parse_key_combo(combo)
    events: list[dict] = []
    held = 0
    for name in modifiers:
        held |= MODIFIER_BITS[name]
        modifier = _SPECIAL[name]
        events.append(_event("rawKeyDown", modifier, held))
    text = definition.text
    if text and held & MODIFIER_BITS["Shift"] and text.isalpha():
        text = text.upper()
    # Shortcuts such as Control+A send no text, so the page sees a command.
    if held & (MODIFIER_BITS["Control"] | MODIFIER_BITS["Alt"] | MODIFIER_BITS["Meta"]):
        text = ""
    down = _event("keyDown" if text else "rawKeyDown", definition, held)
    if text:
        down["text"] = text
        down["unmodifiedText"] = text
        if definition.key.isalpha() and len(definition.key) == 1:
            down["key"] = text
    events.append(down)
    events.append(_event("keyUp", definition, held))
    for name in reversed(modifiers):
        held &= ~MODIFIER_BITS[name]
        events.append(_event("keyUp", _SPECIAL[name], held))
    return events


def _event(kind: str, definition: KeyDefinition, modifiers: int) -> dict:
    return {
        "type": kind,
        "key": definition.key,
        "code": definition.code,
        "windowsVirtualKeyCode": definition.key_code,
        "nativeVirtualKeyCode": definition.key_code,
        "modifiers": modifiers,
    }
