const LABELS = {
  connected: 'Connected to Monaw',
  connecting: 'Connecting…',
  waiting: 'Waiting for Monaw',
  unpaired: 'Not set up',
};

function render(status) {
  const state = status?.state || 'waiting';
  document.getElementById('state').textContent = LABELS[state] || state;
  document.getElementById('detail').textContent = status?.detail || '';
  document.getElementById('dot').className = `dot ${state}`;
}

async function refresh() {
  const { status } = await chrome.storage.session.get('status');
  render(status);
}

document.getElementById('version').textContent = `Version ${chrome.runtime.getManifest().version}`;
document.getElementById('reconnect').addEventListener('click', async () => {
  render({ state: 'connecting' });
  await chrome.runtime.sendMessage({ type: 'reconnect' });
  await refresh();
});
chrome.storage.session.onChanged.addListener((changes) => {
  if (changes.status) render(changes.status.newValue);
});
refresh();
