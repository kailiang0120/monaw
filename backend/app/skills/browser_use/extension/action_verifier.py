"""Explicit postconditions using fixed observations, never arbitrary verifier code."""
from __future__ import annotations

import asyncio
import json
import time

from .session import ToolError
from .page_state import evaluate_fixed

EFFECT_TYPES = {'url_changed', 'url_equals', 'element_appeared', 'element_disappeared', 'value_equals', 'dialog_opened', 'dialog_closed', 'download_started'}


def validate_expectation(effect):
    if effect is None:
        return
    if not isinstance(effect, dict) or effect.get('type') not in EFFECT_TYPES:
        raise ToolError('Unsupported expected_effect. Use a structured browser condition.')
    if effect['type'] in {'element_appeared', 'element_disappeared', 'value_equals'}:
        if not isinstance(effect.get('selector'), str) or not effect['selector'].strip() or len(effect['selector']) > 1000:
            raise ToolError('expected_effect requires one observed CSS selector of at most 1000 characters.')
    if effect['type'] in {'url_equals', 'value_equals'} and not isinstance(effect.get('value'), str):
        raise ToolError('expected_effect requires a string value.')
    timeout = effect.get('timeout', 2000)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 <= timeout <= 10000:
        raise ToolError('expected_effect timeout must be between 0 and 10000 milliseconds.')


async def verify_effect(tools, session, effect, before):
    if effect is None:
        return {'type':'not_requested', 'passed':None, 'reason':'Input dispatch alone does not establish application success.'}
    kind = effect['type']
    deadline = time.monotonic() + effect.get('timeout', 2000) / 1000
    while True:
        passed = False
        if kind == 'dialog_opened':
            passed = session.dialog is not None
        elif kind == 'dialog_closed':
            passed = session.dialog is None
        elif kind == 'download_started':
            passed = session.download_count > before.get('downloads', 0)
        elif kind.startswith('url_'):
            # Runtime read handles SPA URL transitions even if an event was missed.
            result = await evaluate_fixed(session, 'location.href', timeout=3)
            actual = (result.get('result') or {}).get('value')
            passed = isinstance(actual, str) and (actual == effect.get('value') if kind == 'url_equals' else actual != before.get('url'))
        else:
            if session.dialog is not None:
                return {'type':kind, 'passed':False, 'reason':'A dialog prevents observing the requested effect.'}
            object_id, selector = await tools._dom_target(session, {'selector':effect['selector']})
            try:
                state = await tools._read_element(session, object_id, selector, details=kind == 'value_equals')
            finally:
                await tools._release_dom_target(session, object_id)
            if kind == 'element_appeared':
                passed = state.get('attached') is True and state.get('visible') is True
            elif kind == 'element_disappeared':
                passed = state.get('attached') is False or state.get('visible') is False
            elif kind == 'value_equals':
                # Redacted password values cannot establish equality.
                passed = state.get('value') == effect['value'] and state.get('value') != '[redacted]'
        if passed:
            return {'type':kind, 'passed':True}
        if time.monotonic() >= deadline:
            return {'type':kind, 'passed':False, 'reason':'The expected effect was not observed within the verification budget.'}
        await asyncio.sleep(0.1)
