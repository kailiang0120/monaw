"""Document-bound form navigation protection enforced by Chrome itself.

CSP survives page-side removal of the meta element and delayed submit handlers.
It follows CSP URL matching (queries are ignored, trailing slashes match paths),
so this is a form destination guard, not a general network or script firewall.
"""

from .page_state import isolated_dom_context
from .session import ToolError


_INSTALL = r"""(destination => {
    // Parse in the isolated world and serialize one source, never HTML or directives.
    const url = new URL(destination);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
        !/^[a-zA-Z0-9.-]+$/.test(url.hostname)) {
        throw new Error('Unsupported form destination; no input was sent.');
    }
    const path = url.pathname.replace(/[^a-zA-Z0-9/._~!$&()*+%=:@-]/g, encodeURIComponent);
    const policy = 'form-action ' + url.origin + path;
    const previous = globalThis.__monawFormPolicy;
    if (previous) {
        if (previous.policy !== policy) {
            throw new Error('This document is bound to another form destination. Reload and review before submitting elsewhere.');
        }
        return {installed: true, violations: previous.violations};
    }
    if (!document.head || document.contentType !== 'text/html') {
        throw new Error('Cannot protect form navigation in this document; no input was sent.');
    }
    const state = {policy, violations: 0};
    // Synthetic events cannot manufacture evidence of a browser-blocked request.
    globalThis.addEventListener('securitypolicyviolation', event => {
        if (event.isTrusted && event.effectiveDirective === 'form-action' &&
            event.originalPolicy === policy) {
            state.violations += 1;
        }
    }, true);
    const meta = document.createElement('meta');
    meta.httpEquiv = 'Content-Security-Policy';
    meta.content = policy;
    document.head.append(meta);
    // The browser retains the parsed policy until document replacement.
    meta.remove();
    globalThis.__monawFormPolicy = state;
    return {installed: true, violations: state.violations};
})"""


async def protect_form_submission(attempt) -> None:
    """Install a native policy in the approved form's frame before sending input."""
    import json

    binding = attempt.binding
    if not binding.get("runtime_facts", {}).get("form_submission"):
        return
    if getattr(attempt, "form_policy_context", None) is not None:
        return
    method, params = getattr(attempt, "boundary", (None, {}))
    if attempt.arguments.get("visual_target") and (method != "Input.dispatchMouseEvent" or params.get("type") != "mousePressed"):
        return  # Validate the screenshot before our own DOM mutation installs CSP.
    session = attempt.session
    if session is None:
        raise ToolError("The form document is unavailable; no input was sent.")
    context_id = await isolated_dom_context(session, (binding.get("target") or {}).get("frame_id") or None)
    await session.send("Page.setBypassCSP", {"enabled": False})
    result = await session.send("Runtime.evaluate", {
        "expression": _INSTALL + "(" + json.dumps(binding["destination"]) + ")",
        "contextId": context_id,
        "returnByValue": True,
    })
    if result.get("exceptionDetails"):
        raise ToolError("The form destination could not be protected. Reload the page and review the submission again.")
    state = (result.get("result") or {}).get("value") or {}
    if not isinstance(state, dict) or state.get("installed") is not True:
        raise ToolError("The browser did not confirm form destination protection; no input was sent.")
    attempt.form_policy_context = context_id
    attempt.form_policy_violations = state["violations"]


async def form_submission_blocked(attempt) -> bool:
    """Report native CSP violations without trusting page-created events or URLs."""
    context_id = getattr(attempt, "form_policy_context", None)
    if context_id is None:
        return False
    try:
        result = await attempt.session.send("Runtime.evaluate", {
            "expression": "globalThis.__monawFormPolicy?.violations || 0",
            "contextId": context_id,
            "returnByValue": True,
        })
    except ToolError:
        return False  # Successful document navigation destroys the isolated world.
    value = (result.get("result") or {}).get("value", 0)
    return isinstance(value, int) and value > attempt.form_policy_violations
