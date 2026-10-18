function isExternalOpenableUrl(value) {
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

function isSameOrigin(value, base) {
  try {
    return new URL(value).origin === new URL(base).origin
  } catch {
    return false
  }
}

/**
 * Decide what a window.open or target=_blank request does. Monaw never opens a
 * second app window: renderer and backend URLs are dropped (the renderer
 * previews those in-app), and only web links leave for the system browser.
 */
function windowOpenTarget(url, { isTrustedRendererUrl, backendBaseUrl }) {
  if (isTrustedRendererUrl(url) || isSameOrigin(url, backendBaseUrl)) return 'deny'
  return isExternalOpenableUrl(url) ? 'external' : 'deny'
}

module.exports = {
  isExternalOpenableUrl,
  windowOpenTarget,
}
