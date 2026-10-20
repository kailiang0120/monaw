const assert = require('node:assert/strict')
const test = require('node:test')
const { backendPort, useDevelopmentRenderer } = require('./runtime-options.cjs')

test('isolated launch can select a port without changing the backend host', () => {
  assert.equal(backendPort({ backendPort: 8435 }, {}), 8435)
  assert.equal(backendPort({ backendPort: 8435 }, { MONAW_BACKEND_PORT: '48001' }), 48001)
  for (const value of ['0', '80', '65536', '8435junk', 'https://example.com', '-1', '']) {
    assert.throws(() => backendPort({}, { MONAW_BACKEND_PORT: value }), /Invalid/)
  }
})

test('source launches can exercise the bundle while packaged launches always use it', () => {
  assert.equal(useDevelopmentRenderer(false, {}), true)
  assert.equal(useDevelopmentRenderer(false, { MONAW_RENDERER_MODE: 'bundle' }), false)
  assert.equal(useDevelopmentRenderer(true, { MONAW_RENDERER_MODE: 'dev' }), false)
  assert.throws(() => useDevelopmentRenderer(false, { MONAW_RENDERER_MODE: 'remote' }), /Invalid/)
})
