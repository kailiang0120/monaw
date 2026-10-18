const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const { windowOpenTarget } = require('./navigation-policy')

const mainSource = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8')
const policy = {
  isTrustedRendererUrl: (url) => url.startsWith('http://localhost:5275'),
  backendBaseUrl: 'http://127.0.0.1:8420',
}

test('window.open never opens another app window for in-app URLs', () => {
  assert.equal(windowOpenTarget('http://localhost:5275/', policy), 'deny')
  assert.equal(windowOpenTarget('http://localhost:5275/C:/Users/KL/shot.png', policy), 'deny')
  assert.equal(windowOpenTarget('http://127.0.0.1:8420/api/files/shot-1', policy), 'deny')
})

test('window.open sends only web links to the system browser', () => {
  assert.equal(windowOpenTarget('https://example.com/page', policy), 'external')
  assert.equal(windowOpenTarget('http://example.com', policy), 'external')
  assert.equal(windowOpenTarget('file:///C:/Users/KL/shot.png', policy), 'deny')
  assert.equal(windowOpenTarget('attachment://shot-1', policy), 'deny')
  assert.equal(windowOpenTarget('javascript:alert(1)', policy), 'deny')
  assert.equal(windowOpenTarget('', policy), 'deny')
})

test('main window open handler always denies new windows', () => {
  const handler = mainSource.match(/setWindowOpenHandler\(\(\{ url \}\) => \{[\s\S]*?\n  \}\)/)
  assert.ok(handler, 'setWindowOpenHandler block not found')
  assert.match(handler[0], /windowOpenTarget\(/)
  assert.doesNotMatch(handler[0], /action:\s*'allow'/)
})
