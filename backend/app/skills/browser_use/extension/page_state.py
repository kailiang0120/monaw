"""Fresh page evidence from fixed code; page text is never authorization."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .session import ToolError

PAGE_STATE = r"""(() => {
  const key = '__monaw_page_state_v1__';
  if (!globalThis[key]) {
    const state = {revision: 0};
    new MutationObserver(() => state.revision++).observe(document, {subtree:true, childList:true, attributes:true, characterData:true});
    addEventListener('scroll', () => state.revision++, true);
    addEventListener('resize', () => state.revision++, true);
    Object.defineProperty(globalThis, key, {value:state});
  }
  const state = globalThis[key], view = visualViewport;
  return {revision:state.revision, url:location.href,
    width:innerWidth, height:innerHeight, scale:devicePixelRatio,
    scrollX, scrollY, visualScale:view ? view.scale : 1,
    offsetX:view ? view.offsetLeft : 0, offsetY:view ? view.offsetTop : 0};
})()"""


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


async def isolated_dom_context(session, frame_id: str | None = None) -> int:
    """Fixed browser queries use pristine DOM wrappers, never page accessors.

    Arbitrary evaluate_script intentionally does not use this realm. Merely
    hardcoding a JS function is insufficient if its receiver came from the
    page's JavaScript world, where native properties can have forged getters.
    """
    frame_id = frame_id or session.main_frame_id
    frame = session.frames.get(frame_id) or {}
    loader = str(frame.get('loaderId') or (session.loader_id if frame_id == session.main_frame_id else ''))
    key = (frame_id, loader, session.connection_epoch)
    worlds = getattr(session, '_dom_worlds', {})
    context_id = worlds.get(key)
    if context_id:
        return context_id
    world = await session.send('Page.createIsolatedWorld', {'frameId':frame_id,
        'worldName':'monaw-browser-dom', 'grantUniveralAccess':False}, timeout=5)
    context_id = world.get('executionContextId')
    if not isinstance(context_id, int):
        raise ToolError('inaccessible_frame: a trusted DOM inspection realm is unavailable.')
    # Only retain the current document per frame and connection epoch.
    session._dom_worlds = {k:v for k,v in worlds.items() if k[0] != frame_id and k[2] == session.connection_epoch}
    session._dom_worlds[key] = context_id
    return context_id


async def evaluate_fixed(session, expression: str, *, return_by_value: bool = True, timeout: float = 5):
    return await session.send('Runtime.evaluate', {'expression':expression,
        'contextId':await isolated_dom_context(session), 'returnByValue':return_by_value}, timeout=timeout)


def frame_origin(frame: dict[str, Any]) -> str | None:
    """Prefer CDP's effective origin, including inherited about:blank/srcdoc.

    An explicit opaque origin must never fall back to a superficially HTTP URL.
    Such child frames are omitted from snapshots and cannot supply action UIDs.
    """
    from app.agent.action_security.destination_policy import canonical_origin
    if frame.get('_monaw_effective_origin'):
        return str(frame['_monaw_effective_origin'])
    security_origin = frame.get('securityOrigin')
    candidate = security_origin if security_origin not in (None, '') else frame.get('url')
    try:
        return canonical_origin(str(candidate or ''))
    except ValueError:
        return None


async def resolve_frame_origins(session) -> None:
    # Chrome can serialize inherited about:blank/srcdoc securityOrigin as ://.
    # Ask that frame's isolated realm for its effective origin; never infer it
    # from a parent because a sandboxed frame can have an opaque origin.
    for frame_id, frame in list(session.frames.items()):
        if frame_origin(frame) is not None or frame.get('url') not in {'about:blank', 'about:srcdoc'}:
            continue
        try:
            world = await session.send('Page.createIsolatedWorld', {'frameId':frame_id,
                'worldName':'monaw-browser-origin', 'grantUniveralAccess':False}, timeout=3)
            if not world.get('executionContextId'):
                continue
            result = await session.send('Runtime.evaluate', {'expression':'globalThis.origin',
                'contextId':world['executionContextId'], 'returnByValue':True}, timeout=3)
            from app.agent.action_security.destination_policy import canonical_origin
            frame['_monaw_effective_origin'] = canonical_origin(str((result.get('result') or {}).get('value') or ''))
        except Exception:
            continue  # Remains explicitly unsupported; no inherited-origin guess.


async def read_page_state(session) -> dict[str, Any]:
    key = (session.main_frame_id, session.loader_id, session.connection_epoch)
    if getattr(session, '_observer_context_key', None) != key:
        world = await session.send('Page.createIsolatedWorld', {'frameId':session.main_frame_id,
            'worldName':'monaw-browser-evidence', 'grantUniveralAccess':False}, timeout=5)
        session._observer_context_id = world.get('executionContextId')
        session._observer_context_key = key
    context_id = getattr(session, '_observer_context_id', None)
    if not context_id:
        return {}
    result = await session.send('Runtime.evaluate', {'expression':PAGE_STATE, 'returnByValue':True, 'contextId':context_id}, timeout=5)
    value = (result.get('result') or {}).get('value')
    if not isinstance(value, dict) or not isinstance(value.get('revision'), int):
        return {}  # Unsupported inspection never produces a fresh visual binding.
    # CDP context + loader identity works on ordinary HTTP as well as HTTPS;
    # no page crypto API or page-writable token is trusted for freshness.
    value['document'] = canonical_hash({'frame':session.main_frame_id, 'loader':session.loader_id,
                                       'epoch':session.connection_epoch, 'context':context_id})
    return value


def context_version(session, state: dict[str, Any]) -> str:
    return canonical_hash({'tab':session.tab_id, 'loader':session.loader_id, 'frame':session.main_frame_id,
                           'epoch':session.connection_epoch, 'navigation':session.same_document, 'state':state})
