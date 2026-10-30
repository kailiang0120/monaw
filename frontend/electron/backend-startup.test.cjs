const assert = require('node:assert/strict')
const net = require('node:net')
const test = require('node:test')
const { portInUse, startupFailureMessage } = require('./backend-startup.cjs')

test('detects a port another program is listening on, and a free one', async () => {
  const other = net.createServer()
  await new Promise((resolve) => other.listen(0, '127.0.0.1', resolve))
  const { port } = other.address()
  try {
    assert.equal(await portInUse('127.0.0.1', port), true)
  } finally {
    await new Promise((resolve) => other.close(resolve))
  }
  assert.equal(await portInUse('127.0.0.1', port), false)
})

test('startup failures explain what happened without stopping anything for the user', () => {
  const busy = startupFailureMessage({ kind: 'port_in_use' }, { port: 8435, logPath: 'C:\\logs\\backend.log' })
  assert.equal(busy.message, 'Port 8435 is already in use')
  assert.match(busy.detail, /Monaw did not stop that program/)

  const crashed = startupFailureMessage({ kind: 'exited', code: 3 }, { port: 8435, logPath: 'C:\\logs\\backend.log' })
  assert.match(crashed.detail, /exited with code 3/)
  assert.match(crashed.detail, /C:\\logs\\backend\.log/)
})
