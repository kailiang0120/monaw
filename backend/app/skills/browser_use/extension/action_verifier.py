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


# Fixed, read-only query for the state a fill should have produced. Values are
# compared in Python; nothing from the model is sent into the page.
FILLED_STATE = r"""function() {
  const el = this.nodeType === 3 ? this.parentElement : this;
  if (!el || !el.isConnected) return {kind: 'detached'};
  if (el.matches('input[type="password"], input[type="hidden"]') || /^cc-/.test(el.getAttribute('autocomplete') || '')) return {kind: 'protected'};
  if (el instanceof HTMLSelectElement) return {kind: 'select', options: Array.from(el.selectedOptions).map(o => [o.value, o.textContent.trim()])};
  const role = el.getAttribute('role') || '';
  if (el instanceof HTMLInputElement && (el.type === 'checkbox' || el.type === 'radio')) return {kind: 'toggle', checked: el.checked};
  if (['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio'].includes(role)) return {kind: 'toggle', checked: el.getAttribute('aria-checked') === 'true'};
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return {kind: 'text', value: String(el.value).slice(0, 20000)};
  if (el.isContentEditable) return {kind: 'rich', value: String(el.innerText).slice(0, 20000)};
  return {kind: 'unknown'};
}"""

_AUTO_VERIFIED = {'fill', 'fill_form'}


def _compare_filled(state, expected: str):
    kind = state.get('kind') if isinstance(state, dict) else None
    if kind == 'text':
        value = str(state.get('value'))
        # Inputs drop line breaks, and email/url inputs trim, when a value is set.
        sanitized = expected.replace('\r', '').replace('\n', '')
        if value == expected or value in {sanitized, sanitized.strip()}:
            return True, None
        return False, str(state.get('value'))[:200]
    if kind == 'rich':
        observed = ' '.join(str(state.get('value') or '').split())
        return (True, None) if observed == ' '.join(expected.split()) else (False, observed[:200])
    if kind == 'select':
        selected = state.get('options') or []
        if any(value == expected or label == expected.strip() for value, label in selected):
            return True, None
        return False, ', '.join(label for _value, label in selected)[:200]
    if kind == 'toggle' and expected in {'true', 'false'}:
        checked = bool(state.get('checked'))
        return (True, None) if checked == (expected == 'true') else (False, str(checked).lower())
    return None, {'protected': 'Protected field values are not read back.',
                  'detached': 'The field was replaced after filling; take a snapshot to check it.'}.get(
                      kind, 'This field type cannot be read back.')


async def auto_verify(tools, session, name, arguments):
    """Read back what a fill wrote when the model gave no expected_effect.

    Only fixed read-only queries are used, and no input is ever repeated.
    """
    if name not in _AUTO_VERIFIED:
        return await verify_effect(tools, session, None, {})
    if session.dialog is not None:
        return {'type':'value_equals', 'automatic':True, 'passed':None, 'reason':'A dialog prevents reading the field back.'}
    fields = ([{'uid':arguments.get('uid'), 'value':arguments.get('value')}] if name == 'fill'
              else [item for item in arguments.get('elements') or [] if isinstance(item, dict)])
    results = []
    for field in fields:
        uid, expected = str(field.get('uid')), str(field.get('value') or '')
        try:
            state = await tools._call_on(session, tools._node(session, uid), FILLED_STATE)
        except ToolError:
            state = {'kind':'detached'}
        passed, detail = _compare_filled(state, expected)
        entry = {'uid':uid, 'passed':passed}
        if passed is False:
            entry['observed'] = detail
        elif passed is None:
            entry['reason'] = detail
        results.append(entry)
    verdicts = [entry['passed'] for entry in results]
    passed = False if False in verdicts else True if verdicts and all(verdicts) else None
    outcome = {'type':'value_equals', 'automatic':True, 'passed':passed, 'fields':results}
    if passed is False:
        outcome['reason'] = 'A field does not hold the requested value; the page may have reformatted or rejected it.'
    elif passed is None:
        outcome['reason'] = 'Some fields could not be read back.'
    return outcome
