from __future__ import annotations

import inspect
import time
from collections.abc import Mapping
from typing import Any


SOURCE_REFERENCE = {
    "dom_service": "browser_use.dom.service.DomService",
    "snapshot": "browser_use.dom.enhanced_snapshot",
    "serializer": "browser_use.dom.serializer.serializer",
    "clickable_detector": "browser_use.dom.serializer.clickable_elements",
}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _get(value: Any, *names: str, default: Any = "") -> Any:
    if value is None:
        return default
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _safe_str(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    text = str(value)
    return text[:limit]


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = _safe_str(value).strip()
        if text:
            return text
    return ""


def _attributes(node: Any) -> dict[str, str]:
    raw = _get(node, "attributes", "attrs", default={})
    if isinstance(raw, Mapping):
        return {str(key): _safe_str(value, 300) for key, value in raw.items()}
    if isinstance(raw, list):
        items: dict[str, str] = {}
        for index in range(0, len(raw) - 1, 2):
            items[str(raw[index])] = _safe_str(raw[index + 1], 300)
        return items
    return {}


def _node_snapshot(node: Any) -> Any:
    return _get(node, "snapshot_node", "enhanced_snapshot_node", "snapshot", default=None)


def _node_ax(node: Any) -> Any:
    return _get(node, "accessibility_node", "ax_node", "ax", default=None)


def _ax_property(ax_node: Any, name: str) -> Any:
    properties = _get(ax_node, "properties", default={})
    if isinstance(properties, Mapping):
        value = properties.get(name)
        if isinstance(value, Mapping):
            return value.get("value", value.get("raw_value", value))
        return value
    if isinstance(properties, list):
        for item in properties:
            item_dict = _as_dict(item)
            if item_dict.get("name") == name:
                raw = item_dict.get("value")
                if isinstance(raw, Mapping):
                    return raw.get("value", raw.get("raw_value", raw))
                return raw
    return None


def _bounds(snapshot_node: Any, node: Any) -> dict[str, Any]:
    raw = _get(snapshot_node, "bounds", "bounding_box", "rect", default=None) or _get(node, "bounds", "rect", default=None)
    if isinstance(raw, Mapping):
        x = float(raw.get("x", raw.get("left", 0)) or 0)
        y = float(raw.get("y", raw.get("top", 0)) or 0)
        width = float(raw.get("width", raw.get("w", 0)) or 0)
        height = float(raw.get("height", raw.get("h", 0)) or 0)
    elif isinstance(raw, (list, tuple)) and len(raw) >= 4:
        x, y, width, height = (float(raw[0] or 0), float(raw[1] or 0), float(raw[2] or 0), float(raw[3] or 0))
    else:
        x = float(_get(node, "x", default=0) or 0)
        y = float(_get(node, "y", default=0) or 0)
        width = float(_get(node, "width", default=0) or 0)
        height = float(_get(node, "height", default=0) or 0)
    return {
        "x": round(x, 2),
        "y": round(y, 2),
        "width": round(width, 2),
        "height": round(height, 2),
        "center_x": round(x + width / 2, 2),
        "center_y": round(y + height / 2, 2),
    }


def _state_value(node: Any, ax_node: Any, attributes: dict[str, str], name: str) -> bool:
    attr_value = attributes.get(name) or attributes.get(f"aria-{name}")
    if attr_value is not None and attr_value != "":
        return attr_value.lower() not in {"false", "0"}
    return _safe_bool(_get(node, name, default=_ax_property(ax_node, name)))


def _control_metadata(node: Any, attributes: dict[str, str], tag: str) -> dict[str, Any]:
    input_type = _first_non_empty(_get(node, "input_type", "type"), attributes.get("type"))
    control: dict[str, Any] = {
        "input_type": input_type,
        "value_preview": _safe_str(_first_non_empty(_get(node, "value"), attributes.get("value")), 120),
        "accept": _safe_str(attributes.get("accept", "")),
        "multiple": _safe_bool(attributes.get("multiple")),
        "contenteditable": _safe_str(attributes.get("contenteditable", _get(node, "contenteditable", default=""))),
    }
    options = _get(node, "options", default=None)
    if isinstance(options, list):
        control["option_count"] = len(options)
    elif tag == "select":
        control["option_count"] = int(_get(node, "option_count", default=0) or 0)
    return {key: value for key, value in control.items() if value not in {"", False, 0}}


def _context_metadata(node: Any, snapshot_node: Any) -> dict[str, Any]:
    return {
        "in_iframe": _safe_bool(_get(node, "in_iframe", default=_get(snapshot_node, "in_iframe", default=False))),
        "cross_origin_iframe": _safe_bool(_get(node, "cross_origin_iframe", default=False)),
        "shadow_root": _safe_str(_get(node, "shadow_root_type", "shadow_root", default="")),
        "obscured_or_filtered": _safe_bool(_get(node, "obscured_or_filtered", "paint_filtered", default=False)),
    }


def _selector_from_attributes(tag: str, attributes: dict[str, str]) -> str:
    element_id = attributes.get("id", "").strip()
    if element_id:
        escaped_id = element_id.replace("\\", "\\\\").replace('"', '\\"')
        return f"#{escaped_id}"
    name = attributes.get("name", "").strip()
    if tag and name:
        escaped_name = name.replace("\\", "\\\\").replace('"', '\\"')
        return f'{tag}[name="{escaped_name}"]'
    return ""


def normalize_serialized_state(
    state: Any,
    *,
    limit: int,
    include_tree: bool = False,
    source: str = "upstream",
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    selector_map = _get(state, "selector_map", "selectorMap", default={})
    nodes: list[tuple[str, Any]] = []
    if isinstance(selector_map, Mapping):
        nodes = [(str(key), value) for key, value in selector_map.items()]
    elif isinstance(selector_map, list):
        nodes = [(str(index), value) for index, value in enumerate(selector_map)]

    elements: list[dict[str, Any]] = []
    ref_cache: dict[str, dict[str, Any]] = {}
    for index, (source_index, node) in enumerate(nodes, start=1):
        node_dict = _as_dict(node)
        snapshot_node = _node_snapshot(node)
        ax_node = _node_ax(node)
        attributes = _attributes(node)
        tag = _first_non_empty(_get(node, "tag_name", "tag", "node_name"), node_dict.get("tag_name"), node_dict.get("tag")).lower()
        role = _first_non_empty(_get(ax_node, "role"), attributes.get("role"), _get(node, "role"))
        ax_name = _first_non_empty(_get(ax_node, "name"), _get(node, "ax_name"), attributes.get("aria-label"))
        bounds = _bounds(snapshot_node, node)
        ref = f"b{index}"
        css_selector = _first_non_empty(
            _get(node, "css_selector", "selector"),
            _get(snapshot_node, "css_selector", "selector"),
            _selector_from_attributes(tag, attributes),
        )
        labels = _get(node, "labels", default=[])
        if not isinstance(labels, list):
            labels = [_safe_str(labels)] if labels else []
        text = _safe_str(_first_non_empty(_get(node, "text"), _get(node, "inner_text"), ax_name), 300)
        states = {
            "checked": _state_value(node, ax_node, attributes, "checked"),
            "expanded": _state_value(node, ax_node, attributes, "expanded"),
            "selected": _state_value(node, ax_node, attributes, "selected"),
            "disabled": _state_value(node, ax_node, attributes, "disabled"),
            "required": _state_value(node, ax_node, attributes, "required"),
            "invalid": _state_value(node, ax_node, attributes, "invalid"),
            "editable": _safe_bool(_get(node, "is_editable", "editable", default=_ax_property(ax_node, "editable"))),
        }
        element = {
            "ref": ref,
            "source_index": source_index,
            "tag": tag,
            "role": role,
            "type": _first_non_empty(attributes.get("type"), _get(node, "type")),
            "text": text,
            "value": _safe_str(_first_non_empty(_get(node, "value"), attributes.get("value")), 120),
            "name": _safe_str(attributes.get("name", "")),
            "id": _safe_str(attributes.get("id", "")),
            "title": _safe_str(attributes.get("title", "")),
            "aria_label": _safe_str(attributes.get("aria-label", ax_name)),
            "placeholder": _safe_str(attributes.get("placeholder", "")),
            "labels": [_safe_str(item, 120) for item in labels if _safe_str(item).strip()][:4],
            "contenteditable": _safe_str(attributes.get("contenteditable", "")),
            "target_hint": _safe_str(_get(node, "target_hint", default="")),
            "backend_node_id": _get(snapshot_node, "backend_node_id", "backendNodeId", default=_get(node, "backend_node_id", default="")),
            "session_id": _safe_str(_get(node, "session_id", default="")),
            "target_id": _safe_str(_get(node, "target_id", default="")),
            "frame_id": _safe_str(_get(node, "frame_id", default="")),
            "xpath": _safe_str(_get(node, "xpath", default=_get(snapshot_node, "xpath", default=""))),
            "css_selector": css_selector,
            "ax_name": ax_name,
            "ax_description": _safe_str(_get(ax_node, "description", default=_get(node, "ax_description", default=""))),
            "states": states,
            "bounds": bounds,
            "scroll": {
                "scrollable": _safe_bool(_get(node, "is_scrollable", "scrollable", default=False)),
                "can_scroll_vertical": _safe_bool(_get(node, "can_scroll_vertical", default=False)),
                "can_scroll_horizontal": _safe_bool(_get(node, "can_scroll_horizontal", default=False)),
            },
            "context": _context_metadata(node, snapshot_node),
            "control": _control_metadata(node, attributes, tag),
            "x": bounds["x"],
            "y": bounds["y"],
            "width": bounds["width"],
            "height": bounds["height"],
        }
        elements.append(element)
        ref_cache[ref] = {
            "ref": ref,
            "source": source,
            "source_index": source_index,
            "css_selector": css_selector,
            "xpath": element["xpath"],
            "backend_node_id": element["backend_node_id"],
            "session_id": element["session_id"],
            "target_id": element["target_id"],
            "frame_id": element["frame_id"],
            "metadata": element,
        }
        if len(elements) >= max(1, min(limit, 200)):
            break

    metadata = {
        "engine": "enhanced_cdp",
        "source": source,
        "source_reference": SOURCE_REFERENCE,
        "element_count_before_filter": len(nodes),
        "element_count_after_filter": len(elements),
        "fallback_reason": "",
    }
    snapshot: dict[str, Any] = {
        "elements": elements,
        "inspection_metadata": metadata,
    }
    if include_tree:
        tree_text = _safe_str(_get(state, "tree_text", "clickable_elements_to_string", default=""), 20000)
        if tree_text:
            snapshot["tree_text"] = tree_text
    return snapshot, ref_cache


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_state_method(owner: Any, method_name: str) -> Any:
    method = getattr(owner, method_name, None)
    if not callable(method):
        return None
    try:
        signature = inspect.signature(method)
        kwargs: dict[str, Any] = {}
        for name in signature.parameters:
            if name in {"include_screenshot", "cache_clickable_elements_hashes"}:
                kwargs[name] = False
        return await _maybe_await(method(**kwargs))
    except TypeError:
        return await _maybe_await(method())


def _candidate_owners(page: Any, manager: Any) -> list[Any]:
    owners = [page]
    for attr in ("browser_session", "session", "_browser_session", "_session"):
        owner = getattr(page, attr, None)
        if owner is not None:
            owners.append(owner)
    browser = getattr(manager, "_browser", None)
    if browser is not None:
        owners.append(browser)
        for attr in ("browser_session", "session", "_browser_session", "_session"):
            owner = getattr(browser, attr, None)
            if owner is not None:
                owners.append(owner)
    deduped = []
    seen: set[int] = set()
    for owner in owners:
        marker = id(owner)
        if marker not in seen:
            seen.add(marker)
            deduped.append(owner)
    return deduped


async def inspect_page_with_upstream(
    page: Any,
    manager: Any,
    *,
    limit: int,
    engine: str = "auto",
    include_tree: bool = False,
    include_scroll_info: bool = True,
    include_hidden_hints: bool = True,
) -> dict[str, Any]:
    del include_scroll_info, include_hidden_hints
    normalized_engine = str(engine or "auto").strip().lower()
    if normalized_engine == "legacy":
        return {"status": "skipped", "reason_code": "legacy_requested"}

    started = time.perf_counter()
    errors: list[str] = []
    owners = _candidate_owners(page, manager)
    for owner in _candidate_owners(page, manager):
        for method_name in (
            "get_serialized_dom_state",
            "get_dom_state",
            "get_state",
            "get_browser_state",
            "get_page_state",
        ):
            try:
                state = await _call_state_method(owner, method_name)
            except Exception as exc:
                errors.append(f"{type(owner).__name__}.{method_name}: {exc}")
                continue
            if state is None:
                continue
            state_dict = _as_dict(state)
            if not (state_dict.get("selector_map") or hasattr(state, "selector_map")):
                continue
            snapshot, ref_cache = normalize_serialized_state(
                state,
                limit=limit,
                include_tree=include_tree,
                source=f"{type(owner).__name__}.{method_name}",
            )
            snapshot["inspection_metadata"]["timings_ms"] = {
                "adapter": round((time.perf_counter() - started) * 1000, 2)
            }
            return {"status": "ok", "snapshot": snapshot, "ref_cache": ref_cache}

    try:
        from browser_use.dom.service import DomService
    except Exception as exc:
        errors.append(f"browser_use.dom.service import: {exc}")
    else:
        for owner in owners:
            if not any(hasattr(owner, attr) for attr in ("get_or_create_cdp_session", "session_manager", "agent_focus_target_id")):
                continue
            try:
                service = DomService(
                    owner,
                    cross_origin_iframes=bool(getattr(manager, "config", {}).get("cross_origin_iframes", False)),
                    paint_order_filtering=bool(getattr(manager, "config", {}).get("paint_order_filtering", True)),
                    max_iframes=int(getattr(manager, "config", {}).get("max_iframes", 5) or 0),
                    max_iframe_depth=int(getattr(manager, "config", {}).get("max_iframe_depth", 2) or 0),
                )
                result = await service.get_serialized_dom_tree()
            except Exception as exc:
                errors.append(f"DomService.get_serialized_dom_tree: {exc}")
                continue
            state = result[0] if isinstance(result, tuple) and result else result
            if not (_as_dict(state).get("selector_map") or hasattr(state, "selector_map")):
                errors.append("DomService returned no selector_map")
                continue
            snapshot, ref_cache = normalize_serialized_state(
                state,
                limit=limit,
                include_tree=include_tree,
                source="DomService.get_serialized_dom_tree",
            )
            timings = result[2] if isinstance(result, tuple) and len(result) > 2 and isinstance(result[2], dict) else {}
            snapshot["inspection_metadata"]["timings_ms"] = {
                **{str(key): round(float(value), 2) for key, value in timings.items() if isinstance(value, (int, float))},
                "adapter": round((time.perf_counter() - started) * 1000, 2),
            }
            cache_setter = getattr(owner, "set_selector_map", None)
            if callable(cache_setter):
                try:
                    await _maybe_await(cache_setter(_get(state, "selector_map", default={})))
                except Exception:
                    pass
            return {"status": "ok", "snapshot": snapshot, "ref_cache": ref_cache}

    return {
        "status": "error",
        "reason_code": "enhanced_dom_unavailable",
        "error": "; ".join(errors[-4:]) or "No upstream-compatible DOM state method was found.",
    }
