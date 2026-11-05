"""Browser tools implemented over the extension's DevTools relay."""

from __future__ import annotations

import asyncio
import json
import time
import secrets
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bridge import ExtensionBridge, ExtensionError
from .chrome import ensure_extension_connected
from .dom_inspection import ACTION_TARGET, READ_ELEMENT
from .keys import key_events
from .session import PageRegistry, SnapshotNode, TabSession, ToolError
from .snapshot import take_snapshot
from .tools_schema import TOOL_NAMES
from .action_executor import ActionAttempt, before_dispatch, current_attempt
from .action_verifier import validate_expectation, verify_effect
from .browser_recovery import recovery_reason
from .locator import validate_identity
from .page_state import canonical_hash, context_version, read_page_state, frame_origin, resolve_frame_origins, isolated_dom_context, evaluate_fixed
from .visual_target import VisualTarget
from .visual_fallback import VISUAL_HIT_TEST

DEFAULT_NAVIGATION_TIMEOUT_S = 30.0
DEFAULT_WAIT_FOR_TIMEOUT_S = 15.0
DEFAULT_ACTION_TIMEOUT_S = 5.0
MAX_SCRIPT_RESULT_CHARS = 20_000
_MUTATING = {"click", "hover", "drag", "fill", "fill_form", "type_text", "press_key", "upload_file",
             "evaluate_script", "handle_dialog", "navigate_page", "new_page", "close_page"}
_TARGET_FACTS = r"""function(action) {
 let el = this.nodeType === 3 ? this.parentElement : this;
 if (!el || !el.isConnected) return null;
 if (action === 'click' && !el.matches('option,input[type="checkbox"],input[type="radio"]')) {
   const r=el.getBoundingClientRect(), hit=el.ownerDocument.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
   if (hit && (hit === el || el.contains(hit))) el=hit;
 }
 if (action === 'click' || action === 'visual') {
   const label=el.closest('label');
   if (label && label.control) el=label.control;
   el=el.closest('button,input,select,textarea,a[href],option') || el;
 }
 const form = el.form || el.closest('form');
 const native = (prototype,key,node) => Object.getOwnPropertyDescriptor(prototype,key).get.call(node);
 // HTMLFormElement named children can shadow action/method/elements even in
 // an isolated realm. Read the actual native properties used by submission.
 const controls = form ? Array.from(native(HTMLFormElement.prototype,'elements',form)) : [];
 const submitter = form && !['submit','image'].includes(el.type)
   ? controls.find(n => n.type === 'submit' || n.type === 'image') : el;
 const endpoint = form ? (submitter && submitter.hasAttribute('formaction') ? submitter.formAction : native(HTMLFormElement.prototype,'action',form)) : '';
 const method = form ? (submitter && submitter.hasAttribute('formmethod') ? submitter.formMethod : native(HTMLFormElement.prototype,'method',form)) : '';
 const rect = el.getBoundingClientRect();
 return {tag:el.localName, type:el.type || '', id:el.id || '', role:el.getAttribute('role') || '',
   href:el.href || '', form_action:endpoint, form_method:method, form_id:form ? Element.prototype.getAttribute.call(form,'id') || '' : '',
   control_index:form ? controls.indexOf(el) : -1,
   current_value:typeof el.value === 'string' ? el.value : null,
   bounds:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},
   selection_start:el.selectionStart, selection_end:el.selectionEnd,
   form_controls:controls.map(n => ({id:n.id, name:n.name, type:n.type, autocomplete:n.autocomplete || '', value:n.value || '', checked:!!n.checked,
     files:n.files ? Array.from(n.files).map(f=>({name:f.name,size:f.size,last_modified:f.lastModified})) : []})),
   attached:el.isConnected, frame_url:el.ownerDocument.location.href};
}"""
CLOSE_PAGE_ERROR = "The last open page cannot be closed. It is fine to keep it open."
# These tools would hang while a JavaScript dialog blocks the page.
_DIALOG_BLOCKED = {
    "take_snapshot", "inspect_element", "take_screenshot", "wait_for", "click", "hover", "drag", "fill", "fill_form",
    "type_text", "press_key", "upload_file", "evaluate_script",
}

_FILL_PREPARE = r"""function(value) {
  const el = this.nodeType === Node.TEXT_NODE ? this.parentElement : this;
  if (!el) return {kind: 'error', message: 'This element cannot be filled.'};
  el.scrollIntoView({block: 'center', inline: 'nearest'});
  if (el instanceof HTMLSelectElement) {
    const options = Array.from(el.options);
    const match = options.find(o => o.value === value) || options.find(o => o.textContent.trim() === value.trim());
    if (!match) {
      return {kind: 'error', message: `No option "${value}". Options: ` + options.map(o => o.textContent.trim()).slice(0, 40).join(', ')};
    }
    el.value = match.value;
    match.selected = true;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return {kind: 'done'};
  }
  const role = (el.getAttribute && el.getAttribute('role')) || '';
  const isInput = el instanceof HTMLInputElement;
  const toggle = (isInput && (el.type === 'checkbox' || el.type === 'radio'))
    || ['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio'].includes(role);
  if (toggle) {
    if (value !== 'true' && value !== 'false') {
      return {kind: 'error', message: 'Use "true" or "false" for checkboxes, radio buttons and switches.'};
    }
    const checked = isInput ? el.checked : el.getAttribute('aria-checked') === 'true';
    if (checked !== (value === 'true')) el.click();
    return {kind: 'done'};
  }
  if ((isInput || el instanceof HTMLTextAreaElement) && (el.readOnly || el.disabled)) {
    return {kind: 'error', message: 'The field is read-only or disabled.'};
  }
  if (isInput && ['date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'].includes(el.type)) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return {kind: 'done'};
  }
  el.focus();
  if (isInput || el instanceof HTMLTextAreaElement) {
    try { el.select(); } catch (error) { el.value = ''; }
    return {kind: 'type'};
  }
  if (el.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(el);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return {kind: 'type'};
  }
  return {kind: 'error', message: 'This element cannot be filled. Click it, or fill a text field, select or checkbox.'};
}"""

# Custom radios and checkboxes (Apple's colour swatches, for example) hide the
# real <input> and draw its <label> instead. Clicking the label is what a
# person does, so use the label's centre when the control has no size.
_LABEL_CENTER = r"""function() {
  const el = this.nodeType === Node.TEXT_NODE ? this.parentElement : this;
  if (!el) return null;
  const labels = [];
  if (el.labels) labels.push(...el.labels);
  const enclosing = el.closest && el.closest('label');
  if (enclosing) labels.push(enclosing);
  const labelledBy = (el.getAttribute && el.getAttribute('aria-labelledby')) || '';
  for (const id of labelledBy.split(/\s+/).filter(Boolean)) {
    const target = document.getElementById(id);
    if (target) labels.push(target);
  }
  for (const label of labels) {
    label.scrollIntoView({block: 'center', inline: 'nearest'});
    const rect = label.getBoundingClientRect();
    if (rect.width > 1 && rect.height > 1) {
      return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
    }
  }
  return null;
}"""

_SELECT_OPTION = r"""function() {
  const select = this.closest && this.closest('select');
  if (!select) return false;
  select.value = this.value;
  this.selected = true;
  select.dispatchEvent(new Event('input', {bubbles: true}));
  select.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}"""


class ElementNotVisible(ToolError):
    """The element has no box on screen (for example a visually hidden input)."""


@dataclass
class ToolOutput:
    lines: list[str] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)
    is_error: bool = False
    action_outcome: dict[str, Any] | None = None
    structured_content: dict[str, Any] = field(default_factory=dict)

    def add(self, line: str) -> None:
        if line:
            self.lines.append(line)


def _timeout_seconds(value: Any, default: float) -> float:
    try:
        milliseconds = int(value or 0)
    except (TypeError, ValueError):
        return default
    return milliseconds / 1000.0 if milliseconds > 0 else default


def _page_id(arguments: dict[str, Any]) -> int | None:
    value = arguments.get("pageId")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"pageId must be a number from list_pages, not {value!r}.") from None


class BrowserTools:
    def __init__(self, bridge: ExtensionBridge, *, require_authorization: bool = False) -> None:
        self.bridge = bridge
        self.pages = PageRegistry(bridge)
        self.require_authorization = require_authorization
        self._contexts: dict[str, tuple[float, str]] = {}
        self._call_lock = asyncio.Lock()

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        # The MCP server owns this lock. A preflight never authorizes; only the
        # parent policy supplies its opaque one-use binding on the real call.
        async with self._call_lock:
            return await self._call(name, arguments)

    async def _call(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        if name == "__monaw_action_context":
            await ensure_extension_connected(self.bridge)
            target_name = str(arguments.get("tool_name") or "")
            target_args = arguments.get("arguments") or {}
            if target_name not in TOOL_NAMES or not isinstance(target_args, dict) or "__monaw_authorization" in target_args:
                raise ToolError("Invalid browser context request.")
            context = await self._build_context(target_name, target_args, prepare_target=True, upload_scope=arguments.get("upload_scope"))
            token = secrets.token_urlsafe(32)
            # Pending tokens expire and are bounded; no replay after a crash.
            now = time.monotonic()
            self._contexts = {key:value for key,value in self._contexts.items() if now - value[0] < 300}
            if len(self._contexts) >= 128:
                self._contexts.pop(next(iter(self._contexts)))
            self._contexts[token] = (now, canonical_hash(context))
            context["context_token"] = token
            return ToolOutput(structured_content={"browser_action_context":context})
        if name not in TOOL_NAMES:
            raise ToolError(f"Unknown tool {name}.")
        await ensure_extension_connected(self.bridge)
        handler = getattr(self, f"_tool_{name}")
        validate_expectation(arguments.get("expected_effect"))
        binding = arguments.get("__monaw_authorization") or {}
        material_args = {key:value for key,value in arguments.items() if key != "__monaw_authorization"}
        attempt = ActionAttempt(name, material_args, str(binding.get("action_id") or "browser_" + secrets.token_hex(12)),
                                guard=self._guard_dispatch, started=time.monotonic())
        attempt.binding = binding
        context_token = current_attempt.set(attempt)
        try:
            if name not in _MUTATING and self.require_authorization:
                await self._guard_dispatch(attempt)
            output = await handler(material_args)
            if name in _MUTATING and attempt.dispatched:
                try:
                    verification = await verify_effect(self, attempt.session, material_args.get("expected_effect"), attempt.before)
                    status = "verified" if verification["passed"] is True else "failed" if verification["passed"] is False else "executed"
                    if output.is_error:
                        status = "uncertain"
                except (ToolError, ExtensionError, TimeoutError):
                    verification = {"type":(material_args.get("expected_effect") or {}).get("type", "not_requested"),
                                    "passed":None, "reason":"The action was dispatched, but observation is unavailable."}
                    status = "uncertain"
                output.action_outcome = self._outcome(attempt, status, verification)
            return output
        except (ExtensionError, ToolError, TimeoutError) as exc:
            if attempt.dispatched:
                # A lost response is reconciled by read-only observations;
                # the input itself is never replayed to obtain proof.
                if material_args.get("expected_effect") and attempt.session is not None:
                    try:
                        verification = await verify_effect(self, attempt.session, material_args["expected_effect"], attempt.before)
                        if verification.get("passed") is True:
                            return ToolOutput(["The browser response was interrupted, but the requested effect was independently verified. No input was repeated."],
                                action_outcome=self._outcome(attempt, "verified", verification))
                    except (ToolError, ExtensionError, TimeoutError):
                        pass
                return ToolOutput(["Browser input may have taken effect; its result could not be confirmed. Observe the page before continuing; do not repeat this operation."],
                    action_outcome=self._outcome(attempt, "uncertain", {"type":"unavailable", "passed":None}, exc))
            if isinstance(exc, ExtensionError):
                raise ToolError("The browser connection is unavailable. Refresh page evidence before trying a new action.") from exc
            raise
        finally:
            current_attempt.reset(context_token)

    @staticmethod
    def _outcome(attempt, status, verification, error=None):
        outcome = {"status":status, "action_id":attempt.action_id, "action_executed":attempt.dispatched,
                   "verification":verification, "diagnostics":{"locator_method":"visual" if attempt.arguments.get("visual_target") else "backend_dom_node",
                    "snapshot_revision":getattr(attempt.session, "snapshot_id", 0), "retry_count":0,
                    "duration_ms":round((time.monotonic() - attempt.started) * 1000)}}
        if status in {"failed", "uncertain"}:
            outcome["recovery"] = recovery_reason(error or RuntimeError("verification_failed" if status == "failed" else "action_effect_uncertain"), dispatched=True)
        return outcome

    async def _guard_dispatch(self, attempt, *, continuation=False):
        binding = attempt.binding
        if not binding and not self.require_authorization:
            return  # Direct in-process test adapter; production stdio is strict.
        if continuation:
            await self._validate_continuation(attempt)
            await self._validate_input_boundary(attempt)
            return
        token = binding.get("context_token")
        stored = self._contexts.pop(token, None)
        if not stored or time.monotonic() - stored[0] > 300:
            raise ToolError("Browser authorization is missing, expired or already consumed; refresh the exact action context.")
        current = await self._build_context(attempt.name, attempt.arguments, upload_scope=binding.get("data_scope") if attempt.name == "upload_file" else None)
        expected = {key:value for key,value in binding.items() if key not in {"context_token", "action_id"}}
        if canonical_hash(current) != stored[1] or canonical_hash(expected) != stored[1]:
            raise ToolError("Browser action context changed after authorization. No action was dispatched; obtain a fresh review.")
        await self._validate_input_boundary(attempt)

    async def _validate_input_boundary(self, attempt):
        method, params = getattr(attempt, "boundary", (None, {}))
        session, target = attempt.session, attempt.binding.get("target") or {}
        if session is None:
            return
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed":
            if attempt.arguments.get("visual_target"):
                record = session.visual_targets.get(attempt.arguments["visual_target"]["screenshot_id"])
                state = await read_page_state(session)
                if record is None or state != record.state:
                    raise ToolError("unsafe_visual_fallback: page or viewport changed before pointer dispatch.")
                node = SnapshotNode("visual", session.loader_id, int(target["backend_node_id"]), "", "")
                same = await self._call_on(session, node, "function(x,y) { return document.elementFromPoint(x,y) === this; }", params["x"], params["y"])
                if same is not True:
                    raise ToolError("unsafe_visual_fallback: pointer hit target changed before dispatch.")
            elif target.get("uid"):
                node = self._node(session, target["uid"])
                state = await self._call_on(session, node, ACTION_TARGET, "click")
                if not isinstance(state, dict) or not state.get("ready"):
                    raise ToolError("target_obscured: target stopped being actionable before pointer dispatch.")
                try:
                    x, y, width, height = await self._box(session, node)
                    hit = x <= params["x"] <= x + width and y <= params["y"] <= y + height
                except ElementNotVisible:
                    point = await self._call_on(session, node, _LABEL_CENTER)
                    hit = isinstance(point, dict) and abs(point.get("x", -999) - params["x"]) < 1 and abs(point.get("y", -999) - params["y"]) < 1
                if not hit:
                    raise ToolError("stale_element: target moved before pointer dispatch; no replacement was clicked.")
        if method in {"Input.insertText", "Input.dispatchKeyEvent"}:
            active_uid = getattr(attempt, "active_uid", None)
            if active_uid:
                node = self._node(session, active_uid)
            elif target.get("backend_node_id") and attempt.name in {"type_text", "press_key"}:
                node = SnapshotNode("focused", session.loader_id, int(target["backend_node_id"]), "", "")
            else:
                return
            focused = await self._call_on(session, node, "function() { const el = this.nodeType === 3 ? this.parentElement : this; return !!el && (el.ownerDocument.activeElement === el || el.contains(el.ownerDocument.activeElement)); }")
            if focused is not True:
                raise ToolError("The focused element changed; remaining keyboard input was stopped.")

    @staticmethod
    def _safe_facts(facts):
        if not isinstance(facts, dict):
            return facts
        # Form values are material context, but raw password/hidden/recipient
        # values must not appear in approvals or safe diagnostics.
        facts = dict(facts)
        facts["dom_role"] = facts.pop("role", "")
        if "current_value" in facts:
            facts["target_value_hash"] = canonical_hash(facts.pop("current_value"))
        facts["form_controls"] = [{**{key:value for key,value in item.items() if key != "value"},
                                   "value_hash":canonical_hash(item.get("value", ""))}
                                  for item in facts.get("form_controls") or []]
        return facts

    async def _validate_continuation(self, attempt):
        """Batch actions keep their exact targets across every inner dispatch.

        Expected field values can change; identity, origin, destinations and
        other material form values must still match the one reviewed action.
        """
        session = attempt.session
        binding = attempt.binding
        if session is None or attempt.name == "new_page":
            return
        await session.refresh_frames()
        await resolve_frame_origins(session)
        original = binding.get("target") or {}
        if session.current_url != binding.get("url"):
            raise ToolError("Browser destination changed during the operation; remaining inputs were stopped.")
        targets = original.get("fields") or ([original] if original.get("backend_node_id") else [])
        if original.get("drop_target"):
            targets = [*targets, original["drop_target"]]
        desired = {}
        updates = attempt.arguments.get("elements") or ([{"uid":attempt.arguments.get("uid"), "value":attempt.arguments.get("value", "")}] if attempt.name == "fill" else [])
        for update in updates:
            target = next((item for item in targets if item.get("uid") == update.get("uid")), None)
            if target and target.get("control_index", -1) >= 0:
                desired[(target.get("frame_id"), target.get("form_id"), target["control_index"])] = canonical_hash(str(update.get("value", "")))
        if attempt.name in {"type_text", "press_key"} and original.get("planned_value_hash") and original.get("control_index", -1) >= 0:
            desired[(original.get("frame_id"), original.get("form_id"), original["control_index"])] = original["planned_value_hash"]
        for target in targets:
            node = self._node(session, str(target["uid"])) if target.get("uid") else SnapshotNode("focused", session.loader_id, int(target["backend_node_id"]), "", "")
            if target.get("uid"):
                await validate_identity(session, node)
            current = self._safe_facts(await self._call_on(session, node, _TARGET_FACTS, attempt.name))
            if not isinstance(current, dict) or not current.get("attached"):
                raise ToolError("A reviewed browser target was replaced during the operation.")
            for key in ("tag", "type", "id", "dom_role", "href", "form_action", "form_method", "form_id", "frame_url", "control_index"):
                if key in target and current.get(key) != target.get(key):
                    raise ToolError("A reviewed browser target or destination changed; remaining inputs were stopped.")
            old_controls, new_controls = target.get("form_controls") or [], current.get("form_controls") or []
            if len(old_controls) != len(new_controls):
                raise ToolError("The reviewed form changed; remaining inputs were stopped.")
            for index, (old, new) in enumerate(zip(old_controls, new_controls)):
                permitted = desired.get((target.get("frame_id"), target.get("form_id"), index))
                if new == old:
                    continue
                adjusted = dict(new)
                if permitted and new.get("value_hash") == permitted:
                    adjusted["value_hash"] = old.get("value_hash")
                if permitted and new.get("type") in {"checkbox", "radio"}:
                    raw = next((str(u.get("value")) for u in updates if any(t.get("uid") == u.get("uid") and t.get("control_index") == index for t in targets)), "")
                    if raw in {"true", "false"} and new.get("checked") == (raw == "true"):
                        adjusted["checked"] = old.get("checked")
                if adjusted != old:
                    raise ToolError("Material form data changed during the operation; remaining inputs were stopped.")

    async def _build_context(self, name, arguments, *, prepare_target=False, upload_scope=None):
        """Runtime evidence returned only to the trusted MCP adapter, never policy instructions."""
        data_scope = {}
        if name in {"list_pages", "new_page"}:
            return {"tool_name":name, "parameters_hash":canonical_hash(arguments), "page_id":None,
                "url":"", "frame_origins":[], "context_version":"browser_new_tab", "target":{"browser":"chrome"},
                "destination":str(arguments.get("url") or ""), "action_kind":"navigate" if name == "new_page" else "read",
                "side_effect":"local_mutation" if name == "new_page" else "none", "data_scope":{},
                "runtime_facts":{"target_verified":True, "context_verified":True}}
        page_id, session = await self._page(arguments, name)
        await session.refresh_frames()
        await resolve_frame_origins(session)
        state = await read_page_state(session) if session.dialog is None else {}
        target = {"page_id":page_id, "tab_id":session.tab_id, "frame_id":session.main_frame_id}
        uid = arguments.get("uid") or arguments.get("from_uid")
        if arguments.get("visual_target"):
            target.update(await self._visual_context(page_id, session, arguments["visual_target"]))
        elif uid is not None:
            node = self._node(session, str(uid))
            await validate_identity(session, node)
            if prepare_target and name in {"click", "fill", "hover", "drag"}:
                await self._wait_actionable(session, node, "fill" if name == "fill" else "click", self._action_deadline(arguments))
            facts = self._safe_facts(await self._call_on(session, node, _TARGET_FACTS, name))
            if not isinstance(facts, dict) or facts.get("attached") is not True:
                raise ToolError("stale_element: target DOM identity is unavailable.")
            target.update({"uid":node.uid, "backend_node_id":node.backend_node_id, "role":node.role,
                           "name":node.name, "frame_id":node.frame_id, "document_id":node.document_id, **facts})
        elif name in {"type_text", "press_key"}:
            obj = await evaluate_fixed(session, "document.activeElement", return_by_value=False)
            handle = (obj.get("result") or {}).get("objectId")
            if not handle:
                raise ToolError("The focused target cannot be inspected reliably.")
            try:
                result = await session.send("Runtime.callFunctionOn", {"objectId":handle, "functionDeclaration":_TARGET_FACTS, "returnByValue":True})
                raw_facts = (result.get("result") or {}).get("value")
                facts = self._safe_facts(raw_facts)
                if not isinstance(facts, dict) or not facts.get("attached"):
                    raise ToolError("The focused target is no longer attached.")
                if facts.get("tag") in {"iframe", "frame"}:
                    raise ToolError("Focused-frame keyboard actions cannot be bound reliably. Use an explicit accessible field uid instead.")
                if isinstance(raw_facts.get("current_value"), str) and isinstance(raw_facts.get("selection_start"), int) and isinstance(raw_facts.get("selection_end"), int):
                    insertion = str(arguments.get("text", "")) if name == "type_text" else str(arguments.get("key", "")) if len(str(arguments.get("key", ""))) == 1 else None
                    if insertion is not None:
                        value = raw_facts["current_value"]
                        facts["planned_value_hash"] = canonical_hash(value[:raw_facts["selection_start"]] + insertion + value[raw_facts["selection_end"]:])
                target.update(facts)
                described = await session.send("DOM.describeNode", {"objectId":handle})
                target["backend_node_id"] = (described.get("node") or {}).get("backendNodeId")
                if not target["backend_node_id"]:
                    raise ToolError("The focused target has no stable DOM identity.")
            finally:
                await self._release_dom_target(session, str(handle))
        if name == "fill_form":
            targets = []
            planned_fields = arguments.get("elements") or []
            if prepare_target and planned_fields:
                await self._wait_actionable(session, self._node(session, str(planned_fields[0].get("uid"))), "fill", self._action_deadline(arguments))
            for item in planned_fields:
                node = self._node(session, str(item.get("uid")))
                await validate_identity(session, node)
                facts = self._safe_facts(await self._call_on(session, node, _TARGET_FACTS))
                if not isinstance(facts, dict) or not facts.get("attached"):
                    raise ToolError("stale_element: a form field is unavailable.")
                targets.append({"uid":node.uid, "backend_node_id":node.backend_node_id, "frame_id":node.frame_id,
                                "document_id":node.document_id, **facts})
            target["fields"] = targets
        if arguments.get("to_uid"):
            node = self._node(session, str(arguments["to_uid"]))
            await validate_identity(session, node)
            target["drop_target"] = {"uid":node.uid, "backend_node_id":node.backend_node_id, "document_id":node.document_id,
                                     **self._safe_facts(await self._call_on(session, node, _TARGET_FACTS))}
            if prepare_target and name == "drag":
                source = self._node(session, str(arguments.get("from_uid")))
                await self._prepare_drag(session, source, node, arguments)
                target.update(self._safe_facts(await self._call_on(session, source, _TARGET_FACTS)))
                target["drop_target"].update(self._safe_facts(await self._call_on(session, node, _TARGET_FACTS)))
        if name == "upload_file":
            from app.agent.action_security.data_scope import revalidate_upload_scope, resolve_upload_scope
            try:
                data_scope = revalidate_upload_scope(upload_scope, workspace_roots=upload_scope.get("workspace_roots")) if upload_scope else resolve_upload_scope(arguments.get("filePaths") or [])
            except (ValueError, OSError) as exc:
                raise ToolError("Upload file scope could not be validated.") from exc
            if target.get("tag") != "input" or target.get("type") != "file":
                raise ToolError("Upload requires a verified input type=file target.")
        url = str(state.get("url") or session.current_url)
        destination = str(target.get("form_action") or target.get("href") or url)
        sensitive_autocomplete = {"cc-name", "cc-number", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc",
                                  "current-password", "new-password", "one-time-code"}
        sensitive_form = any(control.get("type") in {"password", "file"}
                             or bool(set(str(control.get("autocomplete") or "").lower().split()) & sensitive_autocomplete)
                             for control in target.get("form_controls") or [])
        external_submit = (name in {"click", "press_key", "type_text"} and target.get("form_action")
                           and (str(target.get("form_method")).lower() != "get" or sensitive_form)
                           and (name == "click" and target.get("type") in {"submit", "image"}
                                or name == "press_key" and "enter" in str(arguments.get("key")).lower()
                                or name == "type_text" and "enter" in str(arguments.get("submitKey")).lower()))
        kind = "submit" if external_submit else {"evaluate_script":"execute_code", "upload_file":"upload",
                 "navigate_page":"navigate", "fill":"type", "fill_form":"type", "type_text":"type"}.get(name, "click" if name in _MUTATING else "read")
        side_effect = "external_mutation" if external_submit else "unknown" if name in {"evaluate_script", "click", "press_key", "handle_dialog", "drag"} else "local_mutation" if name in _MUTATING else "none"
        if name == "upload_file":
            side_effect = "external_mutation"  # onchange handlers can transmit immediately.
        if name == "navigate_page" and arguments.get("url"):
            destination = str(arguments["url"])
        if session.dialog is not None:
            target["dialog"] = {"type":session.dialog.get("type"), "message_hash":canonical_hash(session.dialog.get("message"))}
        state = await read_page_state(session) if session.dialog is None else {}
        if not state and session.dialog is None:
            raise ToolError("Current document identity is unavailable; browser action cannot be bound safely.")
        return {"tool_name":name, "parameters_hash":canonical_hash(arguments), "page_id":page_id, "tab_id":session.tab_id,
                "url":url, "frame_origins":[origin for frame in session.frames.values() if (origin := frame_origin(frame)) is not None],
                "unsupported_frames":[{"frame_id":frame_id, "reason_code":"opaque_or_unsupported_frame_origin"}
                                      for frame_id, frame in session.frames.items() if frame_origin(frame) is None]
                                     + [{"frame_id":frame_id,"reason_code":"inaccessible_frame"} for frame_id in sorted(session.inaccessible_frames)],
                "context_version":context_version(session, state), "target":target, "destination":destination,
                "action_kind":kind, "side_effect":side_effect, "data_scope":data_scope,
                "runtime_facts":{"target_verified":True, "context_verified":True,
                                 "coordinate_only":bool(arguments.get("visual_target")),
                                 "sensitive_data_submission":bool(external_submit and sensitive_form),
                                 "external_submission":bool(external_submit)}}

    # -- helpers -------------------------------------------------------------

    async def _page(self, arguments: dict[str, Any], tool: str) -> tuple[int, TabSession]:
        page_id, session = await self.pages.resolve(_page_id(arguments))
        await session.prepare()
        if tool in _DIALOG_BLOCKED and session.dialog is not None:
            raise ToolError(self._dialog_message(session))
        attempt = current_attempt.get()
        if attempt is not None and attempt.session is None:
            attempt.session = session
            attempt.before = {"url":session.current_url, "downloads":session.download_count}
        return page_id, session

    @staticmethod
    def _dialog_message(session: TabSession) -> str:
        dialog = session.dialog or {}
        return (
            f'A JavaScript {dialog.get("type") or ""} dialog is open on this page: "{dialog.get("message", "")}". '
            "Use handle_dialog to accept or dismiss it."
        )

    def _node(self, session: TabSession, uid: str) -> SnapshotNode:
        if not session.nodes:
            raise ToolError("No snapshot found for this page. Use take_snapshot to capture one.")
        node = session.nodes.get(str(uid))
        if node is None:
            raise ToolError(f"Element uid {uid} was not found in the latest snapshot. Take a new snapshot.")
        if node.loader_id != session.loader_id and node.frame_id in {"", session.main_frame_id}:
            raise ToolError(f"Element uid {uid} is from before the page navigated. Take a new snapshot.")
        if node.connection_epoch != session.connection_epoch:
            raise ToolError("page_disconnected: the target predates reconnection. Take a fresh snapshot.")
        if node.frame_id and (node.frame_id not in session.frames or session.frames[node.frame_id].get("loaderId", "") != node.document_id):
            raise ToolError("stale_element: the target frame changed or became inaccessible. Take a fresh snapshot.")
        return node

    async def _object_id(self, session: TabSession, node: SnapshotNode, *, isolated: bool = True) -> str:
        try:
            params = {"backendNodeId":node.backend_node_id}
            if isolated:
                params["executionContextId"] = await isolated_dom_context(session, node.frame_id or None)
            result = await session.send("DOM.resolveNode", params)
        except ToolError as exc:
            raise ToolError(f"Element uid {node.uid} no longer exists on the page. Take a new snapshot.") from exc
        object_id = (result.get("object") or {}).get("objectId")
        if not object_id:
            raise ToolError(f"Element uid {node.uid} no longer exists on the page. Take a new snapshot.")
        return str(object_id)

    async def _call_on(self, session: TabSession, node: SnapshotNode, function: str, *args: Any) -> Any:
        object_id = await self._object_id(session, node)
        try:
            result = await session.send("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": function,
                "arguments": [{"value": arg} for arg in args],
                "returnByValue": True,
                "awaitPromise": True,
            })
            if result.get("exceptionDetails"):
                raise ToolError(_exception_text(result["exceptionDetails"]))
            return (result.get("result") or {}).get("value")
        finally:
            await self._release_dom_target(session, object_id)

    async def _center(self, session: TabSession, node: SnapshotNode) -> tuple[float, float]:
        """Where to point the mouse: the element, or its label when the element is hidden."""
        try:
            await session.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node.backend_node_id})
        except ToolError:
            pass
        try:
            box = await self._box(session, node)
        except ElementNotVisible:
            point = await self._call_on(session, node, _LABEL_CENTER)
            if isinstance(point, dict) and "x" in point and "y" in point:
                return float(point["x"]), float(point["y"])
            raise
        return box[0] + box[2] / 2, box[1] + box[3] / 2

    async def _box(self, session: TabSession, node: SnapshotNode) -> tuple[float, float, float, float]:
        try:
            result = await session.send("DOM.getContentQuads", {"backendNodeId": node.backend_node_id})
        except ToolError as exc:
            raise ToolError(f"Element uid {node.uid} no longer exists on the page. Take a new snapshot.") from exc
        best: tuple[float, float, float, float] | None = None
        for quad in result.get("quads") or []:
            xs, ys = quad[0::2], quad[1::2]
            box = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
            if box[2] * box[3] > 1 and (best is None or box[2] * box[3] > best[2] * best[3]):
                best = box
        if best is None:
            raise ElementNotVisible(f"Element uid {node.uid} is not visible (it has no size on screen). Take a new snapshot.")
        return best

    async def _mouse(self, session: TabSession, kind: str, x: float, y: float, **extra: Any) -> bool:
        params = {"type": kind, "x": x, "y": y, **extra}
        if kind == "mouseMoved":
            await session.send("Input.dispatchMouseEvent", params)
            return False
        return await session.send_until_dialog("Input.dispatchMouseEvent", params)

    async def _click_point(self, session: TabSession, x: float, y: float, count: int) -> bool:
        await self._mouse(session, "mouseMoved", x, y)
        for click in range(1, count + 1):
            if await self._mouse(session, "mousePressed", x, y, button="left", buttons=1, clickCount=click):
                return True
            if await self._mouse(session, "mouseReleased", x, y, button="left", buttons=0, clickCount=click):
                return True
        return False

    async def _press(self, session: TabSession, combo: str) -> bool:
        for event in key_events(combo):
            if event["type"] == "keyUp":
                await session.send("Input.dispatchKeyEvent", event)
            elif await session.send_until_dialog("Input.dispatchKeyEvent", event):
                return True
        return False

    async def _insert_text(self, session: TabSession, text: str) -> bool:
        if not text:
            return False
        return await session.send_until_dialog("Input.insertText", {"text": text})

    async def _with_snapshot(self, output: ToolOutput, session: TabSession, include: bool) -> ToolOutput:
        if include and session.dialog is None:
            try:
                output.add(await take_snapshot(session))
            except (ToolError, ExtensionError) as exc:
                # The action already happened. A read failure must not turn it
                # into a failed action that the model might submit again.
                output.add(f"Updated snapshot unavailable: {exc}. Inspect the current page before taking another action; do not repeat the completed action just to get a snapshot.")
        return output

    async def _wait_actionable(self, session: TabSession, node: SnapshotNode, action: str,
                               deadline: float) -> None:
        """Wait before dispatch, never replay an action or substitute a stale uid."""
        observed: dict[str, Any] = {}
        previous_bounds = None
        previous_ready = False
        await validate_identity(session, node)
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                while True:
                    if session.dialog is not None:
                        raise ToolError(self._dialog_message(session))
                    self._node(session, node.uid)
                    observed = await self._call_on(session, node, ACTION_TARGET, action) or {}
                    self._node(session, node.uid)
                    if not observed.get("attached"):
                        raise ToolError(f"Element uid {node.uid} was replaced or removed. Take a new snapshot and choose its current uid; the action was not dispatched.")
                    bounds = observed.get("bounds")
                    if observed.get("ready") and previous_ready and bounds == previous_bounds:
                        return
                    previous_ready = bool(observed.get("ready"))
                    previous_bounds = bounds if observed.get("ready") else None
                    await asyncio.sleep(0.1)
        except TimeoutError as exc:
            safe_state = {key:observed.get(key) for key in ("attached", "visible", "enabled", "covered", "busy", "readonly")}
            raise ToolError(f"Element uid {node.uid} was not ready for {action}; the action was not dispatched. Last state: {json.dumps(safe_state)}. Inspect the element or wait for the blocking condition before trying again.") from exc

    @staticmethod
    def _action_deadline(arguments: dict[str, Any]) -> float:
        return time.monotonic() + min(_timeout_seconds(arguments.get("timeout"), DEFAULT_ACTION_TIMEOUT_S), 30.0)

    async def _wait_for_navigation(self, session: TabSession, loads: int, navigations: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        next_ready_check = 0.0
        while time.monotonic() < deadline:
            if session.load_count > loads or session.dialog is not None:
                return True
            # Pages restored from the back/forward cache fire no load event.
            if session.navigation > navigations and time.monotonic() >= next_ready_check:
                next_ready_check = time.monotonic() + 0.5
                try:
                    state = await evaluate_fixed(session, "document.readyState", timeout=2)
                    if (state.get("result") or {}).get("value") == "complete":
                        return True
                except (ToolError, ExtensionError):
                    pass
            await asyncio.sleep(0.05)
        return False

    async def _run_navigation(self, session: TabSession, method: str, params: dict[str, Any],
                              timeout: float, before_unload: str) -> tuple[dict[str, Any], bool, bool]:
        """Run a navigation command; returns (result, loaded, beforeunload handled)."""
        loads, navigations = session.load_count, session.navigation
        task = asyncio.ensure_future(session.send(method, params, timeout=timeout))
        handled = False
        while not task.done():
            dialog = session.dialog
            if dialog is not None and dialog.get("type") == "beforeunload" and not handled:
                await session.send("Page.handleJavaScriptDialog", {"accept": before_unload != "dismiss"})
                session.dialog = None
                handled = True
            await asyncio.sleep(0.03)
        result = await task
        if result.get("errorText"):
            return result, False, handled
        if method == "Page.navigate" and not result.get("loaderId"):
            return result, True, handled  # Same-document navigation, such as a #fragment.
        loaded = await self._wait_for_navigation(session, loads, navigations, timeout)
        return result, loaded, handled

    # -- pages ---------------------------------------------------------------

    async def _tool_list_pages(self, arguments: dict[str, Any]) -> ToolOutput:
        return ToolOutput([await self.pages.format_pages()])

    async def _tool_select_page(self, arguments: dict[str, Any]) -> ToolOutput:
        page_id, session = await self.pages.resolve(_page_id(arguments))
        if arguments.get("bringToFront"):
            await self.bridge.request("tabs.activate", {"tabId": session.tab_id})
        return ToolOutput([await self.pages.format_pages()])

    async def _tool_new_page(self, arguments: dict[str, Any]) -> ToolOutput:
        url = str(arguments.get("url") or "").strip()
        if not url:
            raise ToolError("A URL is required.")
        await self.pages.list_tabs()  # Existing tabs keep the lower page ids.
        await before_dispatch()
        tab = await self.bridge.request("tabs.create", {"active": not arguments.get("background")})
        tab_id = int((tab or {}).get("id"))
        page_id = self.pages.page_id_for(tab_id)
        self.pages.selected_page = page_id
        session = self.pages.session(tab_id)
        await session.prepare()
        attempt = current_attempt.get()
        if attempt is not None:
            attempt.session = session
        timeout = _timeout_seconds(arguments.get("timeout"), DEFAULT_NAVIGATION_TIMEOUT_S)
        result, loaded, _handled = await self._run_navigation(session, "Page.navigate", {"url": url}, timeout, "accept")
        output = ToolOutput()
        if result.get("errorText"):
            output.add(f"Unable to load {url}: {result['errorText']}.")
        elif not loaded:
            output.add(f"Opened {url}; the page is still loading.")
        output.add(await self.pages.format_pages())
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_navigate_page(self, arguments: dict[str, Any]) -> ToolOutput:
        page_id, session = await self._page(arguments, "navigate_page")
        kind = arguments.get("type") or ("url" if arguments.get("url") else "")
        if not kind:
            raise ToolError("Either URL or a type is required.")
        timeout = _timeout_seconds(arguments.get("timeout"), DEFAULT_NAVIGATION_TIMEOUT_S)
        before_unload = str(arguments.get("handleBeforeUnload") or "accept")
        output = ToolOutput()
        if kind == "url":
            url = str(arguments.get("url") or "").strip()
            if not url:
                raise ToolError("A URL is required for navigation of type=url.")
            result, loaded, handled = await self._run_navigation(session, "Page.navigate", {"url": url}, timeout, before_unload)
            if result.get("errorText"):
                output.add(f"Unable to navigate in the selected page: {result['errorText']}.")
            else:
                output.add(f"Successfully navigated to {url}." + ("" if loaded else " The page is still loading."))
        elif kind == "reload":
            _result, loaded, handled = await self._run_navigation(
                session, "Page.reload", {"ignoreCache": bool(arguments.get("ignoreCache"))}, timeout, before_unload,
            )
            output.add("Successfully reloaded the page." + ("" if loaded else " The page is still loading."))
        elif kind in {"back", "forward"}:
            history = await session.send("Page.getNavigationHistory")
            entries = history.get("entries") or []
            index = int(history.get("currentIndex") or 0) + (-1 if kind == "back" else 1)
            if not 0 <= index < len(entries):
                output.add(f"Unable to navigate {kind} in the selected page: there is no {'previous' if kind == 'back' else 'next'} page.")
                handled = False
            else:
                _result, loaded, handled = await self._run_navigation(
                    session, "Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]}, timeout, before_unload,
                )
                output.add(f"Successfully navigated {kind} to {entries[index].get('url', '')}."
                           + ("" if loaded else " The page is still loading."))
        else:
            raise ToolError(f"Unknown navigation type {kind}.")
        if handled:
            output.add(f"{'Dismissed' if before_unload == 'dismiss' else 'Accepted'} a beforeunload dialog.")
        if session.dialog is not None:
            output.add(self._dialog_message(session))
        output.add(await self.pages.format_pages())
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_close_page(self, arguments: dict[str, Any]) -> ToolOutput:
        tabs = await self.pages.list_tabs()
        page_id = _page_id(arguments)
        tab_id = self.pages.tab_for_page(page_id) if page_id is not None else None
        if tab_id is None:
            raise ToolError(f"No page with id {page_id}. Call list_pages to see open pages.")
        output = ToolOutput()
        if len(tabs) <= 1:
            output.add(CLOSE_PAGE_ERROR)
        else:
            await before_dispatch()
            await self.bridge.request("tabs.close", {"tabId": tab_id})
        output.add(await self.pages.format_pages())
        return output

    # -- reading -------------------------------------------------------------

    async def _tool_take_snapshot(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "take_snapshot")
        return ToolOutput([await take_snapshot(session)])

    async def _dom_target(self, session: TabSession, arguments: dict[str, Any]) -> tuple[str, str | None]:
        uid, selector = arguments.get("uid"), arguments.get("selector")
        if (uid is None) == (selector is None):
            raise ToolError("Provide exactly one uid from the latest snapshot or a specific CSS selector.")
        if uid is not None:
            if not isinstance(uid, str) or not uid.strip():
                raise ToolError("uid must be a non-empty string from the latest snapshot.")
            return await self._object_id(session, self._node(session, uid)), None
        if not isinstance(selector, str) or not selector.strip() or len(selector) > 1000:
            raise ToolError("selector must be a non-empty CSS selector of at most 1000 characters.")
        result = await evaluate_fixed(session, "globalThis", return_by_value=False)
        object_id = (result.get("result") or {}).get("objectId")
        if not object_id:
            raise ToolError("The page is not ready for DOM inspection. Take a new snapshot.")
        return str(object_id), selector

    async def _read_element(self, session: TabSession, object_id: str, selector: str | None,
                            *, details: bool) -> dict[str, Any]:
        result = await session.send("Runtime.callFunctionOn", {
            "objectId": object_id, "functionDeclaration": READ_ELEMENT,
            "arguments": [{"value": selector}, {"value": details}],
            "returnByValue": True,
        }, timeout=5.0)
        if result.get("exceptionDetails"):
            raise ToolError(_exception_text(result["exceptionDetails"]))
        value = (result.get("result") or {}).get("value")
        if not isinstance(value, dict):
            raise ToolError("DOM inspection returned no element state.")
        if value.get("error"):
            raise ToolError(str(value["error"]))
        return value

    async def _release_dom_target(self, session: TabSession, object_id: str) -> None:
        try:
            await session.send("Runtime.releaseObject", {"objectId": object_id}, timeout=1.0)
        except (ToolError, ExtensionError):
            pass  # Navigation or a disconnected tab already released the object.

    async def _tool_inspect_element(self, arguments: dict[str, Any]) -> ToolOutput:
        _, session = await self._page(arguments, "inspect_element")
        object_id, selector = await self._dom_target(session, arguments)
        try:
            result = await self._read_element(session, object_id, selector, details=True)
            if not result.get("attached"):
                raise ToolError("The element is no longer attached or the selector has no matches. Take a new snapshot.")
            return ToolOutput(["## Element inspection\n" + json.dumps(result, ensure_ascii=False, indent=2)])
        finally:
            await self._release_dom_target(session, object_id)

    async def _tool_take_screenshot(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "take_screenshot")
        image_format = str(arguments.get("format") or "png")
        if arguments.get("uid") and arguments.get("fullPage"):
            raise ToolError("uid and fullPage cannot be used together.")
        # Chrome only paints the visible tab, so show it first.
        await self.bridge.request("tabs.activate", {"tabId": session.tab_id})
        params: dict[str, Any] = {"format": image_format}
        if image_format != "png" and arguments.get("quality") is not None:
            params["quality"] = int(arguments["quality"])
        if arguments.get("uid"):
            node = self._node(session, str(arguments["uid"]))
            await self._center(session, node)
            x, y, width, height = await self._box(session, node)
            metrics = await session.send("Page.getLayoutMetrics")
            viewport = metrics.get("cssVisualViewport") or {}
            params["clip"] = {
                "x": x + float(viewport.get("pageX") or 0), "y": y + float(viewport.get("pageY") or 0),
                "width": width, "height": height, "scale": 1,
            }
            params["captureBeyondViewport"] = True
        elif arguments.get("fullPage"):
            metrics = await session.send("Page.getLayoutMetrics")
            size = metrics.get("cssContentSize") or {}
            params["clip"] = {
                "x": 0, "y": 0, "width": float(size.get("width") or 1), "height": float(size.get("height") or 1), "scale": 1,
            }
            params["captureBeyondViewport"] = True
        before_state = await read_page_state(session) if not arguments.get("uid") and not arguments.get("fullPage") else {}
        result = await session.send("Page.captureScreenshot", params, timeout=45.0)
        data = str(result.get("data") or "")
        if not data:
            raise ToolError("Chrome returned an empty screenshot.")
        label = "the element" if arguments.get("uid") else ("the full page" if arguments.get("fullPage") else "the current page viewport")
        output = ToolOutput([f"Took a screenshot of {label}."], [{"data": data, "mimeType": f"image/{image_format}"}])
        if before_state:
            after_state = await read_page_state(session)
            if after_state == before_state:
                import base64
                import io
                from PIL import Image
                with Image.open(io.BytesIO(base64.b64decode(data))) as captured:
                    image_width, image_height = captured.size
                screenshot_id = secrets.token_urlsafe(24)
                session.visual_targets.clear()  # One active viewport, bounded memory.
                session.visual_targets[screenshot_id] = VisualTarget(screenshot_id, _page_id_value, after_state, time.monotonic())
                output.structured_content["visual_binding"] = {"screenshot_id":screenshot_id, "page_id":_page_id_value,
                    "image_width":image_width, "image_height":image_height, "viewport_width":after_state["width"],
                    "viewport_height":after_state["height"], "device_scale":after_state["scale"],
                    "context_version":context_version(session, after_state), "coordinate_space":"viewport_css_pixels",
                    "expires_in_seconds":30}
                output.add("Visual fallback binding: " + json.dumps(output.structured_content["visual_binding"]))
            else:
                output.add("Page changed during capture; this screenshot cannot authorize coordinate targeting. Capture a fresh viewport.")
        return output

    async def _visual_context(self, page_id, session, proposal):
        if not isinstance(proposal, dict) or set(proposal) != {"screenshot_id", "x", "y"}:
            raise ToolError("unsafe_visual_fallback: provide screenshot_id and viewport CSS x/y coordinates.")
        if not session.nodes:
            raise ToolError("unsafe_visual_fallback: attempt a fresh accessibility snapshot before visual targeting.")
        record = session.visual_targets.get(proposal["screenshot_id"])
        if record is None:
            raise ToolError("unsafe_visual_fallback: screenshot binding is unavailable. Capture a fresh viewport.")
        x, y = proposal["x"], proposal["y"]
        if any(not isinstance(v, (float, int)) or isinstance(v, bool) or not math.isfinite(v) for v in (x, y)):
            raise ToolError("unsafe_visual_fallback: coordinates must be finite numbers.")
        state = await read_page_state(session)
        record.validate(page_id, state, x, y)
        global_object = await evaluate_fixed(session, "globalThis", return_by_value=False)
        root = (global_object.get("result") or {}).get("objectId")
        if not root:
            raise ToolError("unsafe_visual_fallback: current hit target is inaccessible.")
        handle = None
        try:
            hit = await session.send("Runtime.callFunctionOn", {"objectId":root, "functionDeclaration":VISUAL_HIT_TEST,
                "arguments":[{"value":x}, {"value":y}]})
            handle = (hit.get("result") or {}).get("objectId")
            if not handle:
                raise ToolError("unsafe_visual_fallback: no visible enabled main-document target. Inaccessible frames are unsupported.")
            described = await session.send("DOM.describeNode", {"objectId":handle})
            backend = (described.get("node") or {}).get("backendNodeId")
            facts = await session.send("Runtime.callFunctionOn", {"objectId":handle, "functionDeclaration":_TARGET_FACTS, "arguments":[{"value":"visual"}], "returnByValue":True})
            target = self._safe_facts((facts.get("result") or {}).get("value"))
            if not backend or not isinstance(target, dict) or not target.get("attached"):
                raise ToolError("unsafe_visual_fallback: exact DOM identity is unavailable; coordinate-only dispatch requires manual review.")
            return {"backend_node_id":backend, "visual_screenshot_id":proposal["screenshot_id"], "x":x, "y":y, **target}
        finally:
            if handle:
                await self._release_dom_target(session, handle)
            await self._release_dom_target(session, root)

    async def _click_visual(self, page_id, session, arguments):
        if arguments.get("uid"):
            raise ToolError("Provide a uid OR a visual_target, not both.")
        proposal = arguments["visual_target"]
        await self._visual_context(page_id, session, proposal)
        # The central action policy gate runs at before_dispatch exactly as for
        # DOM clicks. The one-use screenshot is consumed before any input.
        await before_dispatch()
        session.visual_targets[proposal["screenshot_id"]].consumed = True
        opened = await self._click_point(session, float(proposal["x"]), float(proposal["y"]), 1)
        output = ToolOutput(["Dispatched one click at the freshly hit-tested visual target."])
        if opened:
            output.add(self._dialog_message(session))
        else:
            await session.settle_after_action()
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_wait_for(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "wait_for")
        if "uid" in arguments or "selector" in arguments or "state" in arguments:
            if "text" in arguments:
                raise ToolError("Wait for text OR an element state, not both in one call.")
            return await self._wait_for_element(session, arguments)
        texts = [str(text) for text in arguments.get("text") or [] if str(text)]
        if not texts:
            raise ToolError("Provide at least one text to wait for.")
        timeout = min(_timeout_seconds(arguments.get("timeout"), DEFAULT_WAIT_FOR_TIMEOUT_S), 60.0)
        deadline = time.monotonic() + timeout
        expression = "document.body ? document.body.innerText : ''"
        while True:
            if session.dialog is not None:
                raise ToolError(self._dialog_message(session))
            result = await evaluate_fixed(session, expression)
            body = str((result.get("result") or {}).get("value") or "")
            found = next((text for text in texts if text in body), None)
            if found is not None:
                output = ToolOutput([f"Element matching one of {json.dumps(texts)} found."])
                return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)
            if time.monotonic() >= deadline:
                raise ToolError(f"Timed out after {int(timeout * 1000)} ms waiting for {json.dumps(texts)}.")
            await asyncio.sleep(0.25)

    async def _wait_for_element(self, session: TabSession, arguments: dict[str, Any]) -> ToolOutput:
        state = arguments.get("state", "visible")
        if state not in {"attached", "detached", "visible", "hidden", "enabled", "disabled", "editable", "ready"}:
            raise ToolError("Unknown element state. Use attached, detached, visible, hidden, enabled, disabled, editable, or ready.")
        timeout = min(_timeout_seconds(arguments.get("timeout"), DEFAULT_WAIT_FOR_TIMEOUT_S), 60.0)
        object_id = None
        observed: dict[str, Any] = {}
        loader_id = session.loader_id
        try:
            async with asyncio.timeout(timeout):
                object_id, selector = await self._dom_target(session, arguments)
                while True:
                    if session.dialog is not None:
                        raise ToolError(self._dialog_message(session))
                    if session.loader_id != loader_id:
                        raise ToolError("The page navigated while waiting. Take a new snapshot before choosing another target.")
                    observed = await self._read_element(session, object_id, selector, details=False)
                    matched = (
                        not observed.get("attached") if state == "detached"
                        else not observed.get("visible") if state == "hidden"
                        else observed.get("attached") and not observed.get("enabled") if state == "disabled"
                        else observed.get(state) is True
                    )
                    if matched:
                        output = ToolOutput([f"Element reached state: {state}."])
                        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)
                    await asyncio.sleep(0.1)
        except TimeoutError as exc:
            raise ToolError(f"Timed out waiting for element state {state}. Last observed state: {json.dumps(observed)}") from exc
        finally:
            if object_id:
                await self._release_dom_target(session, object_id)

    async def _tool_list_console_messages(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "list_console_messages")
        messages = session.console
        if not arguments.get("includePreservedMessages"):
            messages = [message for message in messages if message.navigation == session.navigation]
        types = {str(kind) for kind in arguments.get("types") or []}
        if types:
            messages = [message for message in messages if message.type in types]
        size = arguments.get("pageSize")
        index = int(arguments.get("pageIdx") or 0)
        if size:
            messages = messages[index * int(size): (index + 1) * int(size)]
        lines = ["## Console messages"]
        lines += [f"msgid={message.msgid} [{message.type}] {message.text[:500]}" for message in messages]
        if len(lines) == 1:
            lines.append("<no console messages found>")
        return ToolOutput(["\n".join(lines)])

    async def _tool_get_console_message(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "get_console_message")
        msgid = int(arguments.get("msgid") or 0)
        message = next((item for item in session.console if item.msgid == msgid), None)
        if message is None:
            raise ToolError(f"No console message with msgid {msgid}. Call list_console_messages.")
        text = f"msgid={message.msgid} [{message.type}]\n{message.text}"
        if message.stack:
            text += f"\n### Stack trace\n{message.stack}"
        return ToolOutput([text])

    # -- input ---------------------------------------------------------------

    async def _tool_click(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "click")
        if arguments.get("visual_target"):
            return await self._click_visual(_page_id_value, session, arguments)
        node = self._node(session, str(arguments.get("uid")))
        double = bool(arguments.get("dblClick"))
        output = ToolOutput()
        await self._wait_actionable(session, node, "click", self._action_deadline(arguments))
        # A native <select> option cannot be clicked once its popup is drawn by Chrome.
        if node.role == "option":
            await before_dispatch()
            if await self._call_on(session, node, _SELECT_OPTION):
                output.add("Successfully clicked on the element")
                return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)
        x, y = await self._center(session, node)
        opened_dialog = await self._click_point(session, x, y, 2 if double else 1)
        if opened_dialog:
            output.add("The element was clicked and it opened a dialog.")
            output.add(self._dialog_message(session))
            return output
        await session.settle_after_action()
        output.add("Successfully double clicked on the element" if double else "Successfully clicked on the element")
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_hover(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "hover")
        node = self._node(session, str(arguments.get("uid")))
        await self._wait_actionable(session, node, "hover", self._action_deadline(arguments))
        x, y = await self._center(session, node)
        await before_dispatch()
        await self._mouse(session, "mouseMoved", x, y)
        await asyncio.sleep(0.1)
        output = ToolOutput(["Successfully hovered over the element"])
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_drag(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "drag")
        source = self._node(session, str(arguments.get("from_uid")))
        target = self._node(session, str(arguments.get("to_uid")))
        start, end = await self._prepare_drag(session, source, target, arguments)
        await self._mouse(session, "mouseMoved", *start)
        await self._mouse(session, "mousePressed", *start, button="left", buttons=1, clickCount=1)
        steps = 8
        for step in range(1, steps + 1):
            x = start[0] + (end[0] - start[0]) * step / steps
            y = start[1] + (end[1] - start[1]) * step / steps
            await session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "buttons": 1})
            await asyncio.sleep(0.02)
        await self._mouse(session, "mouseReleased", *end, button="left", buttons=0, clickCount=1)
        await session.settle_after_action()
        output = ToolOutput(["Successfully dragged an element"])
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _prepare_drag(self, session, source, target, arguments):
        await self._wait_actionable(session, source, "drag", self._action_deadline(arguments))
        await self._wait_actionable(session, target, "drag", self._action_deadline(arguments))
        # Scrolling to the destination can move the source. Both must fit the
        # current viewport before coordinates are measured or any press begins.
        for node in (source, target):
            state = await self._call_on(session, node, READ_ELEMENT, None, False)
            if not isinstance(state, dict) or not state.get("ready"):
                raise ToolError("Both drag targets must be visible and unobstructed in the same viewport. No input was dispatched.")
        boxes = [await self._box(session, node) for node in (source, target)]
        return tuple((box[0] + box[2] / 2, box[1] + box[3] / 2) for box in boxes)

    async def _fill_one(self, session: TabSession, uid: str, value: str, deadline: float) -> bool:
        """Fill one element; returns whether it opened a dialog."""
        node = self._node(session, uid)
        await self._wait_actionable(session, node, "fill", deadline)
        attempt = current_attempt.get()
        if attempt is not None:
            attempt.active_uid = uid
        previously_dispatched = bool(attempt and attempt.dispatched)
        await before_dispatch()
        prepared = await self._call_on(session, node, _FILL_PREPARE, value) or {}
        kind = prepared.get("kind") if isinstance(prepared, dict) else None
        if kind == "error":
            if attempt is not None:
                attempt.dispatched = previously_dispatched  # Fixed helper proved no field mutation.
            raise ToolError(f"Could not fill uid {uid}: {prepared.get('message')}")
        if kind == "type":
            if value:
                return await self._insert_text(session, value)
            return await self._press(session, "Delete")
        return session.dialog is not None

    async def _tool_fill(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "fill")
        if await self._fill_one(session, str(arguments.get("uid")), str(arguments.get("value") or ""), self._action_deadline(arguments)):
            return ToolOutput(["The element was filled out and it opened a dialog.", self._dialog_message(session)])
        output = ToolOutput(["Successfully filled out the element"])
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_fill_form(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "fill_form")
        elements = [item for item in arguments.get("elements") or [] if isinstance(item, dict)]
        if not elements:
            raise ToolError("Provide at least one element to fill.")
        deadline = self._action_deadline(arguments)
        completed: list[str] = []
        for position, element in enumerate(elements):
            uid = str(element.get("uid"))
            try:
                opened_dialog = await self._fill_one(session, uid, str(element.get("value") or ""), deadline)
            except (ToolError, ExtensionError) as exc:
                if not completed:
                    raise
                output = ToolOutput([
                    f"Form filling stopped at uid {uid}: {exc}",
                    f"Completed uids: {', '.join(completed)}. Remaining uids after this field: "
                    + (', '.join(str(item.get('uid')) for item in elements[position + 1:]) or 'none')
                    + ". Inspect the current field before retrying it; do not repeat completed fields.",
                ], is_error=True)
                return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)
            if opened_dialog:
                remaining = len(elements) - position - 1
                return ToolOutput([
                    f"Filling out the element with uid {uid} opened a dialog."
                    + (f" {remaining} element(s) were not filled." if remaining else ""),
                    self._dialog_message(session),
                ])
            completed.append(uid)
        output = ToolOutput(["Successfully filled out the form"])
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_type_text(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "type_text")
        text = str(arguments.get("text") or "")
        submit = str(arguments.get("submitKey") or "")
        if await self._insert_text(session, text) or (submit and await self._press(session, submit)):
            return ToolOutput(["Typed text into the focused element and it opened a dialog.", self._dialog_message(session)])
        if submit:
            await session.settle_after_action()
        output = ToolOutput(["Dispatched text input to the focused element" + (f" and key {submit}." if submit else ".")])
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_press_key(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "press_key")
        key = str(arguments.get("key") or "")
        if await self._press(session, key):
            return ToolOutput([f"Pressed key {key} and it opened a dialog.", self._dialog_message(session)])
        await session.settle_after_action()
        output = ToolOutput([f"Successfully pressed key: {key}"])
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_handle_dialog(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "handle_dialog")
        if session.dialog is None:
            raise ToolError("No open dialog found")
        accept = arguments.get("action") == "accept"
        params: dict[str, Any] = {"accept": accept}
        if accept and arguments.get("promptText") is not None:
            params["promptText"] = str(arguments["promptText"])
        try:
            await session.send("Page.handleJavaScriptDialog", params)
        except ToolError:
            pass  # Already closed by the user.
        session.dialog = None
        output = ToolOutput(["Successfully accepted the dialog" if accept else "Successfully dismissed the dialog"])
        output.add(await self.pages.format_pages())
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_upload_file(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "upload_file")
        node = self._node(session, str(arguments.get("uid")))
        paths = [str(path) for path in arguments.get("filePaths") or []]
        missing = [path for path in paths if not Path(path).is_absolute() or not Path(path).is_file()]
        if missing:
            raise ToolError("These files do not exist or are not absolute paths: " + ", ".join(missing))
        try:
            await session.send("DOM.setFileInputFiles", {"files": paths, "backendNodeId": node.backend_node_id})
        except ToolError as exc:
            raise ToolError(
                f"Could not upload through uid {node.uid}: {exc}. Choose the page's <input type=file> element from the snapshot."
            ) from exc
        output = ToolOutput(["Files selected in the verified file input. Network upload completion has not been verified."])
        return await self._with_snapshot(output, session, arguments.get("includeSnapshot", True) is not False)

    async def _tool_evaluate_script(self, arguments: dict[str, Any]) -> ToolOutput:
        if arguments.get("pageId") is None:
            raise ToolError("Script execution requires an explicit pageId from list_pages.")
        _page_id_value, session = await self._page(arguments, "evaluate_script")
        function = str(arguments.get("function") or "").strip()
        if not function:
            raise ToolError("A function is required.")
        handles = [
            {"objectId": await self._object_id(session, self._node(session, str(uid)), isolated=False)}
            for uid in arguments.get("args") or []
        ]
        global_object = await session.send("Runtime.evaluate", {"expression": "globalThis"})
        object_id = (global_object.get("result") or {}).get("objectId")
        await before_dispatch()
        result = await session.send("Runtime.callFunctionOn", {
            "functionDeclaration": function,
            "objectId": object_id,
            "arguments": handles,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": True,
        }, timeout=60.0)
        if result.get("exceptionDetails"):
            raise ToolError(f"Script failed: {_exception_text(result['exceptionDetails'])}")
        value = result.get("result") or {}
        if "value" in value:
            text = json.dumps(value["value"], ensure_ascii=False, indent=2)
        else:
            text = str(value.get("description") or value.get("type") or "undefined")
        if len(text) > MAX_SCRIPT_RESULT_CHARS:
            text = text[:MAX_SCRIPT_RESULT_CHARS] + "\n... (truncated)"
        return ToolOutput([f"Script ran on page and returned:\n```json\n{text}\n```"])


def _exception_text(details: dict[str, Any]) -> str:
    exception = details.get("exception") or {}
    return str(exception.get("description") or details.get("text") or "The script threw an exception.")
