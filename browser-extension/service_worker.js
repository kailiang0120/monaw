// Monaw browser bridge.
//
// Relays Chrome DevTools Protocol commands from the Monaw server on this
// computer to tabs through chrome.debugger, and reports the events Monaw needs.
// It holds no policy of its own: Monaw checks permissions and URLs before a
// command reaches this file.

const PROTOCOL_VERSION = 1;
const DEBUGGER_VERSION = '1.3';
const KEEPALIVE_MS = 20_000;
const MAX_RETRY_MS = 30_000;
const RECONNECT_ALARM = 'monaw-reconnect';
const GROUP_TITLE = 'Monaw';
// Only these debugger events are forwarded; the rest would flood the socket.
const FORWARDED_EVENTS = [
  'Page.',
  'Runtime.consoleAPICalled',
  'Runtime.exceptionThrown',
  'Inspector.',
];

let socket = null;
let connecting = false;
let retryTimer = null;
let retryDelay = 1000;
let keepaliveTimer = null;
const attached = new Set();

async function readPairing() {
  try {
    const response = await fetch(chrome.runtime.getURL('pairing.json'), { cache: 'no-store' });
    if (!response.ok) return null;
    const data = await response.json();
    const port = Number(data.port);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) return null;
    if (typeof data.token !== 'string' || data.token.length < 32) return null;
    return { port, token: data.token };
  } catch {
    return null;
  }
}

async function setState(state, detail = '') {
  await chrome.storage.session.set({ status: { state, detail, at: Date.now() } });
  const connected = state === 'connected';
  await chrome.action.setBadgeText({ text: connected ? 'ON' : '' });
  await chrome.action.setBadgeBackgroundColor({ color: connected ? '#16a34a' : '#6b7280' });
}

// A failed WebSocket is logged on the extension's error page, so check the
// port with a quiet no-cors request first.
async function serverReachable(port) {
  try {
    await fetch(`http://127.0.0.1:${port}/health`, { mode: 'no-cors', cache: 'no-store' });
    return true;
  } catch {
    return false;
  }
}

function scheduleRetry() {
  if (retryTimer) return;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect();
  }, retryDelay);
  retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS);
}

async function connect() {
  if (connecting || socket) return;
  connecting = true;
  try {
    const pairing = await readPairing();
    if (!pairing) {
      await setState('unpaired', 'Open Monaw Settings > Browser and click Set up extension.');
      return;
    }
    if (!(await serverReachable(pairing.port))) {
      await setState('waiting', 'Monaw is not running.');
      scheduleRetry();
      return;
    }
    await setState('connecting');
    const ws = new WebSocket(`ws://127.0.0.1:${pairing.port}/extension`);
    socket = ws;
    ws.onopen = () => {
      send(ws, {
        type: 'hello',
        protocol: PROTOCOL_VERSION,
        token: pairing.token,
        version: chrome.runtime.getManifest().version,
        userAgent: navigator.userAgent,
      });
    };
    ws.onmessage = (event) => onMessage(ws, event.data);
    ws.onclose = (event) => onClose(ws, event);
    ws.onerror = () => {};
  } finally {
    connecting = false;
  }
}

function send(ws, message) {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
}

function startKeepalive(ws) {
  stopKeepalive();
  // Socket traffic keeps the service worker alive while Monaw is connected.
  keepaliveTimer = setInterval(() => send(ws, { type: 'ping' }), KEEPALIVE_MS);
}

function stopKeepalive() {
  if (keepaliveTimer) clearInterval(keepaliveTimer);
  keepaliveTimer = null;
}

async function onMessage(ws, raw) {
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    return;
  }
  if (message.type === 'welcome') {
    retryDelay = 1000;
    await setState('connected');
    startKeepalive(ws);
    if (message.reload) chrome.runtime.reload();
    return;
  }
  if (message.type !== 'request' || typeof message.id !== 'number') return;
  try {
    const result = await handle(message.method, message.params || {});
    send(ws, { type: 'response', id: message.id, result: result ?? null });
  } catch (error) {
    send(ws, { type: 'response', id: message.id, error: { message: String(error?.message || error) } });
  }
}

async function onClose(ws, event) {
  if (socket !== ws) return;
  socket = null;
  stopKeepalive();
  // Remove Chrome's debugging banner once Monaw is gone.
  await Promise.all([...attached].map((tabId) => detach(tabId)));
  if (event.code === 4401) {
    await setState('unpaired', 'Monaw rejected the pairing. Run Set up extension again in Monaw Settings > Browser.');
    return;
  }
  if (event.code === 4409) {
    await setState('waiting', 'Another Chrome profile is already connected to Monaw.');
  } else {
    await setState('waiting', 'Monaw disconnected.');
  }
  scheduleRetry();
}

function tabSummary(tab, focusedWindowId) {
  return {
    id: tab.id,
    windowId: tab.windowId,
    url: tab.url || tab.pendingUrl || '',
    title: tab.title || '',
    active: Boolean(tab.active),
    focusedWindow: tab.windowId === focusedWindowId,
    status: tab.status || '',
    groupId: tab.groupId ?? -1,
  };
}

async function focusedWindowId() {
  try {
    return (await chrome.windows.getLastFocused()).id;
  } catch {
    return -1;
  }
}

async function addToGroup(tab) {
  try {
    const [existing] = await chrome.tabGroups.query({ title: GROUP_TITLE, windowId: tab.windowId });
    const groupId = await chrome.tabs.group(
      existing ? { groupId: existing.id, tabIds: [tab.id] } : { tabIds: [tab.id], createProperties: { windowId: tab.windowId } },
    );
    if (!existing) await chrome.tabGroups.update(groupId, { title: GROUP_TITLE, color: 'blue' });
  } catch {
    // Grouping only labels Monaw's tabs; a failure does not affect automation.
  }
}

async function ensureAttached(tabId) {
  if (attached.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, DEBUGGER_VERSION);
  attached.add(tabId);
}

async function detach(tabId) {
  attached.delete(tabId);
  try {
    await chrome.debugger.detach({ tabId });
  } catch {
    // Already detached or closed.
  }
}

function requireTabId(params) {
  const tabId = Number(params.tabId);
  if (!Number.isInteger(tabId) || tabId < 0) throw new Error('A valid tabId is required.');
  return tabId;
}

async function handle(method, params) {
  switch (method) {
    case 'cdp': {
      const tabId = requireTabId(params);
      if (typeof params.method !== 'string' || !params.method) throw new Error('A CDP method is required.');
      await ensureAttached(tabId);
      return chrome.debugger.sendCommand({ tabId }, params.method, params.params || {});
    }
    case 'tabs.list': {
      const windowId = await focusedWindowId();
      const tabs = await chrome.tabs.query({});
      return tabs.map((tab) => tabSummary(tab, windowId));
    }
    case 'tabs.create': {
      const tab = await chrome.tabs.create({ url: 'about:blank', active: params.active !== false });
      await addToGroup(tab);
      return tabSummary(tab, await focusedWindowId());
    }
    case 'tabs.activate': {
      const tab = await chrome.tabs.update(requireTabId(params), { active: true });
      await chrome.windows.update(tab.windowId, { focused: true });
      return tabSummary(tab, tab.windowId);
    }
    case 'tabs.close': {
      const tabId = requireTabId(params);
      await detach(tabId);
      await chrome.tabs.remove(tabId);
      return true;
    }
    case 'debugger.detach':
      await detach(requireTabId(params));
      return true;
    default:
      throw new Error(`Unknown request: ${method}`);
  }
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!socket || source.tabId === undefined) return;
  if (!FORWARDED_EVENTS.some((prefix) => method === prefix || (prefix.endsWith('.') && method.startsWith(prefix)))) return;
  send(socket, { type: 'event', event: 'cdp', tabId: source.tabId, method, params: params || {} });
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source.tabId === undefined) return;
  attached.delete(source.tabId);
  if (socket) send(socket, { type: 'event', event: 'detached', tabId: source.tabId, reason });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  attached.delete(tabId);
  if (socket) send(socket, { type: 'event', event: 'tab.removed', tabId });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'reconnect') {
    retryDelay = 1000;
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = null;
    connect().then(() => sendResponse(true));
    return true;
  }
  return false;
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) connect();
});

chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => connect());

// The alarm wakes the service worker so it reconnects after Monaw starts.
chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: 0.5 });
connect();
