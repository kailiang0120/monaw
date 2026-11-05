"""Fixed, read-only DOM queries shared by inspection and conditional waits."""

# Passed as a function declaration, with selectors supplied as CDP arguments.
# Never interpolate model input into JavaScript or call page event handlers.
READ_ELEMENT = r"""function(selector, details) {
  const doc = this.ownerDocument || this.document;
  let el = this.nodeType === 3 ? this.parentElement : this;
  if (selector !== null) {
    let matches;
    try { matches = doc.querySelectorAll(selector); }
    catch (_) { return {error: 'Invalid CSS selector.'}; }
    if (matches.length > 1) return {error: 'Selector matches multiple elements. Narrow it to one element.'};
    el = matches[0];
  }
  if (!el || !el.isConnected) return {attached: false, visible: false, ready: false};
  const view = doc.defaultView;
  const rect = el.getBoundingClientRect();
  const style = view.getComputedStyle(el);
  const visible = el.checkVisibility
    ? el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) && rect.width > 0 && rect.height > 0
    : rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  const enabled = !el.matches(':disabled') && !el.closest('[aria-disabled="true"], [inert]');
  const editable = enabled && !el.readOnly && !el.closest('[aria-readonly="true"]') && (el.isContentEditable || el.matches('textarea, input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"]):not([type="file"])'));
  const busy = !!el.closest('[aria-busy="true"]');
  const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
  const inViewport = x >= 0 && y >= 0 && x < view.innerWidth && y < view.innerHeight;
  const root = el.getRootNode();
  const hit = inViewport ? (root.elementFromPoint ? root : doc).elementFromPoint(x, y) : null;
  const covered = inViewport && (!hit || !(hit === el || el.contains(hit)));
  const state = {
    attached: true, visible: !!visible, enabled: !!enabled, editable: !!editable, busy,
    inViewport, covered,
    ready: !!(visible && enabled && !busy && inViewport && !covered && style.pointerEvents !== 'none')
  };
  if (!details) return {
    ...state, bounds: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
    coveringElement: covered && hit ? {tag: hit.localName, id: hit.id} : null
  };
  const text = (value, max = 300) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
  const summary = node => node ? {
    tag: node.localName, id: text(node.id), role: node.getAttribute('role'),
    text: text(node.innerText || node.textContent)
  } : null;
  const attributes = {};
  for (const attr of el.attributes) {
    if (['id', 'class', 'name', 'type', 'role', 'placeholder', 'title', 'href', 'for', 'required', 'disabled', 'readonly', 'autocomplete'].includes(attr.name) || attr.name.startsWith('aria-')) {
      if (Object.keys(attributes).length >= 30) break;
      attributes[attr.name] = text(attr.value, 400);
    }
  }
  const describedBy = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean).slice(0, 8);
  const errorIds = (el.getAttribute('aria-errormessage') || '').split(/\s+/).filter(Boolean).slice(0, 8);
  const protectedValue = el.matches('input[type="password"], input[type="hidden"]');
  return {
    ...state, tag: el.localName, attributes, text: text(el.innerText, 1200),
    value: protectedValue ? '[redacted]' : ('value' in el ? text(el.value, 500) : null),
    labels: Array.from(el.labels || []).slice(0, 4).map(label => text(label.innerText)),
    description: describedBy.map(id => text(doc.getElementById(id)?.textContent)).filter(Boolean),
    validation: {
      invalid: el.getAttribute('aria-invalid') === 'true' || !!(el.validity && !el.validity.valid),
      message: text(el.validationMessage),
      errors: errorIds.map(id => text(doc.getElementById(id)?.textContent)).filter(Boolean)
    },
    bounds: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
    coveringElement: covered ? summary(hit) : null,
    parent: summary(el.parentElement),
    previous: summary(el.previousElementSibling), next: summary(el.nextElementSibling),
    children: Array.from(el.children).slice(0, 8).map(summary)
  };
}"""

# Only action tools use this query: it may scroll, but never focuses, clicks,
# types or emits form events. Inspection and explicit waits remain read-only.
ACTION_TARGET = r"""function(action) {
  const inspect = __READ_ELEMENT__;
  const el = this.nodeType === 3 ? this.parentElement : this;
  if (!el || !el.isConnected) return {attached: false, ready: false};
  const original = inspect.call(el, null, false);
  let target = el;
  if (el.localName === 'option') target = el.closest('select') || el;
  // Custom controls can have a hidden input with a visible associated label.
  if (!original.visible && el.matches('input[type="checkbox"], input[type="radio"]')) {
    target = Array.from(el.labels || []).find(label => {
      const rect = label.getBoundingClientRect();
      return rect.width > 1 && rect.height > 1;
    }) || el;
  }
  target.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
  const state = inspect.call(target, null, false);
  const readonly = action === 'fill' && (!!el.readOnly || !!el.closest('[aria-readonly="true"]'));
  return {
    attached: true, visible: state.visible, enabled: original.enabled,
    busy: original.busy || state.busy, covered: state.covered, readonly,
    ready: !!(original.enabled && !original.busy && state.ready && !readonly),
    bounds: state.bounds,
    coveringElement: state.coveringElement ? {tag: state.coveringElement.tag, id: state.coveringElement.id} : null
  };
}""".replace('__READ_ELEMENT__', READ_ELEMENT)
