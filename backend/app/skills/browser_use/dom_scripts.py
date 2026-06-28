from __future__ import annotations

_INTERACTIVE_SELECTOR = (
    "a,button,input,textarea,select,summary,"
    "[role='button'],[role='link'],[role='textbox'],[role='tab'],"
    "[contenteditable='true'],[tabindex]"
)

_STEALTH_INIT_SCRIPT = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  } catch (_) {}
  try {
    window.chrome = window.chrome || { runtime: {} };
  } catch (_) {}
})();
""".strip()

_STEALTH_EVALUATE_SCRIPT = """
() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  } catch (_) {}
  try {
    window.chrome = window.chrome || { runtime: {} };
  } catch (_) {}
  return true;
}
""".strip()

_GLOBAL_DECLUTTER_SCRIPT = """
(options) => {
  const config = options || {};
  const removeFixed = config.removeFixed !== false;
  const clickClose = config.clickClose !== false;
  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const textOf = (el) => normalize([
    el.innerText,
    el.textContent,
    el.getAttribute('aria-label'),
    el.getAttribute('title'),
    el.getAttribute('id'),
    el.getAttribute('class'),
  ].join(' '));
  const lowerTextOf = (el) => textOf(el).toLowerCase();
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return (
      style &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      rect.width > 0 &&
      rect.height > 0 &&
      rect.bottom >= 0 &&
      rect.right >= 0 &&
      rect.top <= (window.innerHeight || document.documentElement.clientHeight || 0) &&
      rect.left <= (window.innerWidth || document.documentElement.clientWidth || 0)
    );
  };
  const removed = [];
  const clicked = [];
  const seen = new Set();
  const pageText = lowerTextOf(document.body || document.documentElement);
  const subscriptionDetected = /\\b(subscription|subscribe|sign\\s*up|newsletter|paywall|premium|register to continue)\\b/.test(pageText);
  const cloudflareDetected = /\\b(cloudflare|checking your browser|verify you are human|access denied|attention required|captcha)\\b/.test(pageText);
  const accessDeniedDetected = /\\b(access denied|403 forbidden|request blocked|not authorized)\\b/.test(pageText);
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;

  const markRemoved = (el, reason) => {
    if (!el || el === document.documentElement || el === document.body || seen.has(el)) return false;
    seen.add(el);
    const rect = el.getBoundingClientRect();
    removed.push({
      reason,
      tag: (el.tagName || '').toLowerCase(),
      text: textOf(el).slice(0, 120),
      x: Math.round(rect.x || 0),
      y: Math.round(rect.y || 0),
      width: Math.round(rect.width || 0),
      height: Math.round(rect.height || 0),
    });
    el.remove();
    return true;
  };

  const bannerPatterns = [
    'cookie', 'consent', 'gdpr', 'privacy', 'newsletter', 'subscribe',
    'subscription', 'paywall', 'modal', 'popup', 'pop-up', 'overlay',
    'adblock', 'advertisement', 'promo', 'registration',
  ];
  const candidateSelectors = [
    '[aria-modal="true"]',
    '[role="dialog"]',
    'dialog',
    '[id*="cookie" i]',
    '[class*="cookie" i]',
    '[id*="consent" i]',
    '[class*="consent" i]',
    '[id*="newsletter" i]',
    '[class*="newsletter" i]',
    '[id*="subscribe" i]',
    '[class*="subscribe" i]',
    '[id*="paywall" i]',
    '[class*="paywall" i]',
    '[id*="modal" i]',
    '[class*="modal" i]',
    '[id*="popup" i]',
    '[class*="popup" i]',
    '[id*="overlay" i]',
    '[class*="overlay" i]',
  ];

  for (const selector of candidateSelectors) {
    for (const el of Array.from(document.querySelectorAll(selector))) {
      if (!isVisible(el)) continue;
      const text = lowerTextOf(el);
      const rect = el.getBoundingClientRect();
      const looksRelevant = bannerPatterns.some((token) => text.includes(token));
      const coversMuch = rect.width >= viewportWidth * 0.35 && rect.height >= Math.min(240, viewportHeight * 0.35);
      if (looksRelevant || coversMuch) markRemoved(el, 'known_banner_or_modal');
    }
  }

  if (clickClose) {
    const closePattern = /^(x|\u00d7|close|dismiss|no thanks|not now|maybe later|continue|continue reading|accept|agree|got it)$/i;
    for (const el of Array.from(document.querySelectorAll('button,a,[role="button"],[aria-label]'))) {
      if (clicked.length >= 4) break;
      if (!isVisible(el)) continue;
      const label = normalize([
        el.innerText,
        el.textContent,
        el.getAttribute('aria-label'),
        el.getAttribute('title'),
      ].join(' '));
      if (!closePattern.test(label)) continue;
      try {
        el.click();
        clicked.push(label.slice(0, 80));
      } catch (_) {}
    }
  }

  if (removeFixed) {
    for (const el of Array.from(document.body ? document.body.querySelectorAll('*') : [])) {
      if (!isVisible(el) || seen.has(el)) continue;
      const style = window.getComputedStyle(el);
      if (!['fixed', 'sticky'].includes(style.position)) continue;
      const rect = el.getBoundingClientRect();
      const zIndex = Number.parseInt(style.zIndex || '0', 10) || 0;
      const spansWidth = rect.width >= viewportWidth * 0.45;
      const edgePinned = rect.top <= 4 || rect.bottom >= viewportHeight - 4;
      const overlaySized = rect.width >= viewportWidth * 0.35 && rect.height >= viewportHeight * 0.18;
      const text = lowerTextOf(el);
      const blockerText = bannerPatterns.some((token) => text.includes(token));
      if ((spansWidth && edgePinned) || overlaySized || zIndex >= 100 || blockerText) {
        markRemoved(el, 'fixed_or_sticky');
      }
    }
  }

  for (const root of [document.documentElement, document.body]) {
    if (!root) continue;
    root.style.setProperty('overflow', 'auto', 'important');
    root.style.setProperty('overflow-y', 'auto', 'important');
    root.style.setProperty('position', 'static', 'important');
  }

  const blockingOverlayCount = Array.from(document.querySelectorAll('[aria-modal="true"],[role="dialog"],dialog'))
    .filter(isVisible)
    .length;

  return {
    removed_count: removed.length,
    clicked_count: clicked.length,
    removed,
    clicked,
    subscription_detected: subscriptionDetected,
    cloudflare_detected: cloudflareDetected,
    access_denied_detected: accessDeniedDetected,
    blocking_overlay_count: blockingOverlayCount,
    ready_state: document.readyState,
    scroll_height: Math.max(
      document.body ? document.body.scrollHeight : 0,
      document.documentElement ? document.documentElement.scrollHeight : 0
    ),
  };
}
""".strip()

_SNAPSHOT_SCRIPT = """
(selector, limit) => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const safeCssEscape = (value) => {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
    return String(value).replace(/["\\\\]/g, '\\\\$&');
  };
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return (
      style &&
      style.visibility !== 'hidden' &&
      style.display !== 'none' &&
      rect.width > 0 &&
      rect.height > 0 &&
      rect.bottom >= 0 &&
      rect.right >= 0 &&
      rect.top <= (window.innerHeight || document.documentElement.clientHeight) &&
      rect.left <= (window.innerWidth || document.documentElement.clientWidth)
    );
  };

  let counter = 1;
  const nextRef = () => {
    let candidate = '';
    do {
      candidate = `b${counter++}`;
    } while (document.querySelector(`[data-agent-ref="${candidate}"]`));
    return candidate;
  };

  const textForId = (id) => {
    if (!id) return '';
    const node = document.getElementById(id);
    return normalize(node ? (node.innerText || node.textContent || '') : '');
  };

  const labelTexts = (el) => {
    const labels = [];
    const push = (value) => {
      const cleaned = normalize(value);
      if (cleaned && !labels.includes(cleaned)) labels.push(cleaned);
    };

    push(el.getAttribute('aria-label'));
    push(el.getAttribute('placeholder'));
    push(el.getAttribute('title'));
    push(el.getAttribute('name'));
    push(el.getAttribute('autocomplete'));

    (el.getAttribute('aria-labelledby') || '').split(/\\s+/).forEach((id) => push(textForId(id)));
    (el.getAttribute('aria-describedby') || '').split(/\\s+/).forEach((id) => push(textForId(id)));
    if (el.labels) Array.from(el.labels).forEach((label) => push(label.innerText || label.textContent));

    const id = el.getAttribute('id');
    if (id) {
      document.querySelectorAll(`label[for="${safeCssEscape(id)}"]`).forEach((label) => {
        push(label.innerText || label.textContent);
      });
    }

    let ancestor = el.parentElement;
    for (let depth = 0; ancestor && depth < 4; depth += 1, ancestor = ancestor.parentElement) {
      push(ancestor.getAttribute('aria-label'));
      if (ancestor.tagName && ancestor.tagName.toLowerCase() === 'label') {
        push(ancestor.innerText || ancestor.textContent);
      }
      const role = ancestor.getAttribute('role') || '';
      if (['group', 'dialog', 'form', 'search'].includes(role)) {
        const heading = ancestor.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"]');
        if (heading) push(heading.innerText || heading.textContent);
      }
    }

    const previous = el.previousElementSibling;
    if (previous) push(previous.innerText || previous.textContent);
    return labels.slice(0, 8);
  };

  const classify = (el, labels) => {
    const tag = (el.tagName || '').toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    const blob = normalize([
      ...labels,
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('name'),
      el.getAttribute('id'),
      el.innerText,
      el.textContent,
    ].join(' ')).toLowerCase();

    if (tag === 'textarea' || el.isContentEditable || role === 'textbox' || tag === 'input') {
      if (/\\b(to|recipient|recipients)\\b/.test(blob)) return 'recipient_field';
      if (/\\b(cc|bcc)\\b/.test(blob)) return 'copy_recipient_field';
      if (/\\bsubject\\b/.test(blob)) return 'subject_field';
      if (/\\b(message|body|compose|content)\\b/.test(blob)) return 'message_body_field';
      if (type === 'email') return 'email_field';
      if (/\\bsearch\\b/.test(blob) || type === 'search') return 'search_field';
      return 'text_field';
    }
    if (tag === 'select') return 'select_field';
    if (tag === 'button' || role === 'button') return 'button';
    if (tag === 'a' || role === 'link') return 'link';
    return '';
  };

  const optionSummary = (el) => {
    if ((el.tagName || '').toLowerCase() !== 'select') return [];
    return Array.from(el.options || []).slice(0, 20).map((option) => ({
      value: option.value,
      text: normalize(option.text || option.label || ''),
      selected: !!option.selected,
      disabled: !!option.disabled,
    }));
  };

  const containerSummary = (container) => {
    if (!container) return '';
    const labelledBy = (container.getAttribute('aria-labelledby') || '').split(/\\s+/).map((id) => textForId(id)).join(' ');
    const heading = container.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"]');
    return normalize([
      container.getAttribute('aria-label'),
      container.getAttribute('title'),
      labelledBy,
      heading ? (heading.innerText || heading.textContent || '') : '',
      container.innerText || container.textContent || '',
    ].join(' ')).slice(0, 220);
  };

  const elementSummary = (el) => {
    if (!el) return null;
    let ref = el.getAttribute('data-agent-ref');
    if (!ref && el.matches && el.matches(selector)) {
      ref = nextRef();
      el.setAttribute('data-agent-ref', ref);
    }
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : { x: 0, y: 0, width: 0, height: 0 };
    return {
      ref: ref || '',
      tag: (el.tagName || '').toLowerCase(),
      role: el.getAttribute('role') || '',
      type: el.getAttribute('type') || '',
      id: el.getAttribute('id') || '',
      name: el.getAttribute('name') || '',
      aria_label: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      text: normalize(el.innerText || el.textContent || '').slice(0, 160),
      value: (el.getAttribute('type') || '').toLowerCase() === 'password' ? '' : normalize(el.value || '').slice(0, 160),
      contenteditable: el.isContentEditable || el.getAttribute('contenteditable') || '',
      x: Math.round(rect.x || 0),
      y: Math.round(rect.y || 0),
      width: Math.round(rect.width || 0),
      height: Math.round(rect.height || 0),
    };
  };

  const visibleContainers = [];
  for (const container of document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog,form')) {
    if (!isVisible(container)) continue;
    const rect = container.getBoundingClientRect();
    visibleContainers.push({
      tag: (container.tagName || '').toLowerCase(),
      role: container.getAttribute('role') || '',
      aria_modal: container.getAttribute('aria-modal') || '',
      aria_label: container.getAttribute('aria-label') || '',
      summary: containerSummary(container),
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    });
    if (visibleContainers.length >= 8) break;
  }

  const elements = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!isVisible(el)) continue;
    let ref = el.getAttribute('data-agent-ref');
    if (!ref) {
      ref = nextRef();
      el.setAttribute('data-agent-ref', ref);
    }
    const rect = el.getBoundingClientRect();
    const labels = labelTexts(el);
    const isPassword = (el.getAttribute('type') || '').toLowerCase() === 'password';
    const container = el.closest('[role="dialog"],[aria-modal="true"],dialog,form');
    const containerText = containerSummary(container);
    const isActive = el === document.activeElement || (el.contains && el.contains(document.activeElement));
    elements.push({
      ref,
      tag: (el.tagName || '').toLowerCase(),
      role: el.getAttribute('role') || '',
      type: el.getAttribute('type') || '',
      id: el.getAttribute('id') || '',
      name: el.getAttribute('name') || '',
      aria_label: el.getAttribute('aria-label') || '',
      labels,
      placeholder: el.getAttribute('placeholder') || '',
      title: el.getAttribute('title') || '',
      text: normalize(el.innerText || el.textContent || ''),
      value: isPassword ? '' : normalize(el.value || ''),
      contenteditable: el.isContentEditable || el.getAttribute('contenteditable') || '',
      target_hint: classify(el, labels),
      options: optionSummary(el),
      href: el.getAttribute('href') || '',
      disabled: !!el.disabled,
      active: !!isActive,
      in_dialog: !!container,
      dialog_label: container ? normalize([
        container.getAttribute('aria-label'),
        container.getAttribute('title'),
        containerText,
      ].join(' ')).slice(0, 220) : '',
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      center_x: Math.round(rect.x + rect.width / 2),
      center_y: Math.round(rect.y + rect.height / 2),
    });
    if (elements.length >= limit) break;
  }
  return {
    url: window.location.href,
    title: document.title || '',
    page_state: {
      loading_state: document.readyState || '',
      is_blank_page: window.location.href === 'about:blank',
      viewport: {
        width: window.innerWidth || document.documentElement.clientWidth || 0,
        height: window.innerHeight || document.documentElement.clientHeight || 0,
        scroll_x: Math.round(window.scrollX || window.pageXOffset || 0),
        scroll_y: Math.round(window.scrollY || window.pageYOffset || 0),
      },
      active_element: elementSummary(document.activeElement),
    },
    visible_dialogs: visibleContainers,
    elements,
  };
}
""".strip()

_MATCH_REF_SCRIPT = """
(selector, targetText, exactMatch, preferEditable) => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const display = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const wanted = normalize(targetText);
  if (!wanted) return '';

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return (
      style &&
      style.visibility !== 'hidden' &&
      style.display !== 'none' &&
      rect.width > 0 &&
      rect.height > 0
    );
  };

  let counter = 1;
  const nextRef = () => {
    let candidate = '';
    do {
      candidate = `b${counter++}`;
    } while (document.querySelector(`[data-agent-ref="${candidate}"]`));
    return candidate;
  };

  const textForId = (id) => {
    if (!id) return '';
    const node = document.getElementById(id);
    return display(node ? (node.innerText || node.textContent || '') : '');
  };

  const labelTexts = (el) => {
    const labels = [];
    const push = (value) => {
      const cleaned = display(value);
      if (cleaned && !labels.includes(cleaned)) labels.push(cleaned);
    };
    push(el.getAttribute('aria-label'));
    push(el.getAttribute('placeholder'));
    push(el.getAttribute('title'));
    push(el.getAttribute('name'));
    (el.getAttribute('aria-labelledby') || '').split(/\\s+/).forEach((id) => push(textForId(id)));
    (el.getAttribute('aria-describedby') || '').split(/\\s+/).forEach((id) => push(textForId(id)));
    if (el.labels) Array.from(el.labels).forEach((label) => push(label.innerText || label.textContent));
    let ancestor = el.parentElement;
    for (let depth = 0; ancestor && depth < 3; depth += 1, ancestor = ancestor.parentElement) {
      push(ancestor.getAttribute('aria-label'));
      if (ancestor.tagName && ancestor.tagName.toLowerCase() === 'label') push(ancestor.innerText || ancestor.textContent);
    }
    return labels;
  };

  const editableScore = (el) => {
    const tag = (el.tagName || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (el.isContentEditable) return 6;
    if (tag === 'textarea') return 6;
    if (tag === 'input') return 5;
    if (role === 'textbox') return 5;
    return 0;
  };

  const matchScore = (value, baseScore) => {
    const normalized = normalize(value);
    if (!normalized) return 0;
    if (normalized === wanted) return baseScore + 25;
    if (!exactMatch && normalized.includes(wanted)) return baseScore;
    return 0;
  };

  let best = { ref: '', score: 0 };
  for (const el of document.querySelectorAll(selector)) {
    if (!isVisible(el)) continue;
    if (preferEditable && editableScore(el) <= 0) continue;
    const haystacks = [
      ...labelTexts(el).map((value) => [value, 40]),
      [el.getAttribute('aria-label'), 45],
      [el.getAttribute('placeholder'), 42],
      [el.getAttribute('name'), 36],
      [el.getAttribute('title'), 34],
      [el.value, 24],
      [el.innerText, 22],
      [el.textContent, 18],
    ];
    let score = 0;
    for (const [value, baseScore] of haystacks) {
      score = Math.max(score, matchScore(value, baseScore));
    }
    if (!score) continue;
    score += editableScore(el);

    let ref = el.getAttribute('data-agent-ref');
    if (!ref) {
      ref = nextRef();
      el.setAttribute('data-agent-ref', ref);
    }
    if (score > best.score) best = { ref, score };
  }
  return best.ref;
}
""".strip()

_ELEMENT_METADATA_SCRIPT = """
(selector) => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const el = document.querySelector(selector);
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  return {
    ref: el.getAttribute('data-agent-ref') || '',
    tag: (el.tagName || '').toLowerCase(),
    role: el.getAttribute('role') || '',
    type: el.getAttribute('type') || '',
    id: el.getAttribute('id') || '',
    name: el.getAttribute('name') || '',
    href: el instanceof HTMLAnchorElement ? (el.href || el.getAttribute('href') || '') : '',
    download: el.getAttribute('download') || '',
    aria_label: el.getAttribute('aria-label') || '',
    placeholder: el.getAttribute('placeholder') || '',
    text: normalize(el.innerText || el.textContent || ''),
    value: (el.getAttribute('type') || '').toLowerCase() === 'password' ? '' : normalize(el.value || ''),
    contenteditable: el.isContentEditable || el.getAttribute('contenteditable') || '',
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  };
}
""".strip()

