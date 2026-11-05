"""Accessibility-tree page snapshots with element uids.

Each line is ``uid=<id> <role> "<name>" <attributes>``, and a node keeps its
uid across snapshots until the document navigates. The format is the one the
browser skill's prompts describe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .session import SnapshotNode, TabSession, ToolError
from .page_state import frame_origin, resolve_frame_origins

MAX_SNAPSHOT_CHARS = 150_000
MAX_DEPTH = 300
# Wrapper roles that only add nesting. They are kept when they carry a name.
_SKIPPED_ROLES = {"generic", "none", "presentation", "LineBreak", "ListMarker"}
# Boolean properties shown by name when true.
_BOOLEAN_PROPERTIES = {
    "busy", "disabled", "focusable", "focused", "modal", "multiline", "multiselectable",
    "readonly", "required", "selected",
}
_VALUE_PROPERTIES = {
    "autocomplete", "haspopup", "level", "orientation", "roledescription", "url",
    "valuemax", "valuemin", "valuetext",
}
_VALUE_ROLES = {"textbox", "searchbox", "combobox", "spinbutton", "slider", "progressbar", "meter"}


@dataclass
class _Item:
    role: str
    name: str
    backend_node_id: int | None
    attributes: dict[str, Any]
    children: list["_Item"] = field(default_factory=list)
    uid: str = ""
    frame_id: str = ""
    document_id: str = ""


def _ax_value(field_value: Any) -> Any:
    return field_value.get("value") if isinstance(field_value, dict) else None


def _attributes(node: dict[str, Any], role: str) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for prop in node.get("properties") or []:
        name = str(prop.get("name") or "")
        value = _ax_value(prop.get("value"))
        if name in _BOOLEAN_PROPERTIES:
            if value is True:
                attributes[name] = True
        elif name == "expanded":
            attributes["expandable"] = True
            if value is True:
                attributes["expanded"] = True
        elif name in {"checked", "pressed"}:
            if value in {True, "true"}:
                attributes[name] = True
            elif value == "mixed":
                attributes[name] = "mixed"
        elif name in _VALUE_PROPERTIES and value not in (None, "", False):
            attributes[name] = value
    value = _ax_value(node.get("value"))
    if role in _VALUE_ROLES and value not in (None, ""):
        attributes["value"] = value
    description = _ax_value(node.get("description"))
    if description:
        attributes["description"] = description
    return attributes


def _build(nodes: dict[str, dict[str, Any]], node_id: str, depth: int) -> list[_Item]:
    node = nodes.get(node_id)
    if node is None or depth > MAX_DEPTH:
        return []
    children = [item for child in node.get("childIds") or [] for item in _build(nodes, str(child), depth + 1)]
    role = str(_ax_value(node.get("role")) or "")
    name = str(_ax_value(node.get("name")) or "").strip()
    if role == "InlineTextBox":
        return []
    if node.get("ignored") or (role in _SKIPPED_ROLES and not name):
        return children
    if role == "StaticText" and not name:
        return children
    backend = node.get("backendDOMNodeId")
    return [_Item(role, name, backend if isinstance(backend, int) else None, _attributes(node, role), children)]


def _redundant_text_children(item: _Item) -> bool:
    if not item.name or not item.children:
        return False
    if any(child.role != "StaticText" or child.children for child in item.children):
        return False
    text = "".join(child.name for child in item.children)
    return "".join(text.split()) == "".join(item.name.split())


def _format(item: _Item, depth: int, lines: list[str]) -> None:
    parts = [f"uid={item.uid}"]
    if item.role and item.role != "StaticText":
        parts.append(item.role)
    if item.name:
        parts.append('"' + item.name.replace("\n", " ") + '"')
    for key in sorted(item.attributes):
        value = item.attributes[key]
        if value is True:
            parts.append(key)
        else:
            parts.append(f'{key}="{value}"')
    lines.append("  " * depth + " ".join(parts))
    if not _redundant_text_children(item):
        for child in item.children:
            _format(child, depth + 1, lines)


def _walk(items: list[_Item]):
    stack = list(reversed(items))
    while stack:
        item = stack.pop()
        yield item
        stack.extend(reversed(item.children))


async def take_snapshot(session: TabSession) -> str:
    loader_before = session.loader_id
    await resolve_frame_origins(session)
    result = await session.send("Accessibility.getFullAXTree", {}, timeout=30.0)
    raw_nodes = result.get("nodes") or []
    nodes = {str(node.get("nodeId")): node for node in raw_nodes if isinstance(node, dict)}
    roots = [node for node in raw_nodes if isinstance(node, dict) and not node.get("parentId")]
    if not roots:
        return "The page has no accessibility tree yet. Wait for it to load and retry."
    items = _build(nodes, str(roots[0].get("nodeId")), 0)
    for item in _walk(items):
        item.frame_id = session.main_frame_id
        item.document_id = session.loader_id
    limitations = ["An inaccessible or policy-restricted frame was omitted; actions inside it are unsupported."] if session.inaccessible_frames else []
    for frame_id, frame in list(session.frames.items()):
        if frame_id == session.main_frame_id:
            continue
        try:
            if frame_origin(frame) is None:
                raise ToolError("unsupported_frame_origin")
            if not frame.get("loaderId"):
                raise ToolError("inaccessible_frame_document")
            frame_result = await session.send("Accessibility.getFullAXTree", {"frameId":frame_id}, timeout=5)
            frame_nodes = frame_result.get("nodes") or []
            frame_roots = [n for n in frame_nodes if not n.get("parentId")]
            if not frame_roots:
                raise ToolError("inaccessible_frame")
            frame_items = _build({str(n.get("nodeId")):n for n in frame_nodes}, str(frame_roots[0].get("nodeId")), 0)
            for item in _walk(frame_items):
                item.frame_id = frame_id
                item.document_id = str(frame.get("loaderId") or "")
            items.extend(frame_items)
        except Exception:
            # An OOPIF may require a separate debugger target, which this relay
            # cannot inspect. Never invent targets or expose its private URL.
            limitations.append("An inaccessible or policy-restricted frame was omitted; actions inside it are unsupported.")
    if session.loader_id != loader_before:
        raise ToolError("The document changed while taking the snapshot. Take a fresh snapshot.")

    # Assign uids: reuse a node's uid while the document is unchanged.
    session.snapshot_id += 1
    counter = 0
    loader_id = session.loader_id
    uid_by_key: dict[str, str] = {}
    session.nodes = {}
    for item in _walk(items):
        key = f"{item.frame_id}_{item.document_id}_{item.backend_node_id}" if item.backend_node_id is not None else ""
        uid = session.uid_by_key.get(key) if key else None
        if uid is None:
            counter += 1
            uid = f"{session.snapshot_id}_{counter}"
        item.uid = uid
        if key:
            uid_by_key[key] = uid
            session.nodes[uid] = SnapshotNode(uid, loader_id, int(item.backend_node_id), item.role, item.name,
                frame_id=item.frame_id, document_id=item.document_id, snapshot_revision=session.snapshot_id,
                connection_epoch=session.connection_epoch, attributes=item.attributes)
    session.uid_by_key = uid_by_key

    lines: list[str] = []
    for item in items:
        _format(item, 0, lines)
    text = "\n".join(lines)
    if len(text) > MAX_SNAPSHOT_CHARS:
        text = text[:MAX_SNAPSHOT_CHARS] + "\n... (snapshot truncated; scroll or narrow the task and take a new snapshot)"
    return "## Latest page snapshot\n" + text + ("\n" + "\n".join(dict.fromkeys(limitations)) if limitations else "")
