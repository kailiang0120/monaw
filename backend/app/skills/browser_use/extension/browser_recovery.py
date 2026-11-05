"""Browser recovery never replays an uncertain input dispatch."""
from __future__ import annotations


def recovery_reason(error: BaseException, *, dispatched: bool) -> dict:
    message = str(error).lower()
    reason = ('extension_disconnected' if 'extension' in message and ('disconnect' in message or 'connected' in message)
              else 'page_disconnected' if any(word in message for word in ('closed', 'detach', 'disconnect'))
              else 'navigation_timeout' if 'navigat' in message and 'timeout' in message
              else 'target_obscured' if 'covered' in message or 'obscur' in message
              else 'target_ambiguous' if 'ambiguous' in message or 'multiple' in message
              else 'stale_element' if 'stale' in message or 'replaced' in message
              else 'unsafe_visual_fallback' if 'visual_fallback' in message
              else 'verification_failed' if 'verification_failed' in message
              else 'action_effect_uncertain' if dispatched else 'verification_failed')
    return {'reason_code':reason, 'retry_allowed':False, 'retry_count':0,
            'next_step':'Observe and reconcile the current page before any further action; do not replay this operation.'
                        if dispatched else 'Refresh page evidence and choose the exact target before a new operation.'}
