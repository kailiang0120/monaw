"""Strict backend-node identity: never substitute an accessible-name match."""
from __future__ import annotations

from .session import ToolError
from .page_state import frame_origin


async def validate_identity(session, node) -> None:
    if node.loader_id != session.loader_id and node.frame_id in ('', session.main_frame_id):
        raise ToolError('stale_element: target is from before the page navigated. Take a new snapshot.')
    if node.connection_epoch != session.connection_epoch:
        raise ToolError('page_disconnected: target predates reconnection. Take a new snapshot.')
    frame = session.frames.get(node.frame_id)
    if node.frame_id and (not frame or frame.get('loaderId', '') != node.document_id):
        raise ToolError('stale_element: target frame changed or became inaccessible. Take a new snapshot.')
    if node.frame_id != session.main_frame_id and frame_origin(frame or {}) is None:
        raise ToolError('inaccessible_frame: opaque or unsupported frame origins cannot be targeted.')
    # Partial AX queries validate the exact backend node, including changed semantics.
    result = await session.send('Accessibility.getPartialAXTree', {'backendNodeId':node.backend_node_id, 'fetchRelatives':False})
    candidates = [item for item in result.get('nodes', [])
                  if item.get('backendDOMNodeId') == node.backend_node_id and not item.get('ignored')]
    if not candidates:
        # One observation refresh is safe: retain the exact backend identity,
        # frame and document. Never resolve a replacement by accessible name.
        refreshed = await session.send('Accessibility.getFullAXTree', {'frameId':node.frame_id} if node.frame_id else {})
        candidates = [item for item in refreshed.get('nodes', [])
                      if item.get('backendDOMNodeId') == node.backend_node_id and not item.get('ignored')]
        if len(candidates) != 1:
            raise ToolError('stale_element: target identity is unavailable after one refresh; take a new snapshot. No action was dispatched.')
    actual = candidates[0]
    role = str((actual.get('role') or {}).get('value') or '')
    name = str((actual.get('name') or {}).get('value') or '').strip()
    if (role, name) != (node.role, node.name):
        raise ToolError('stale_element: target role or accessible name changed. Take a new snapshot; no replacement was selected.')
