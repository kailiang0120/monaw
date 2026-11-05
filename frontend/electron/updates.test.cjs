const { EventEmitter } = require('node:events')
const test = require('node:test')
const assert = require('node:assert/strict')
const { createUpdateController } = require('./updates.cjs')

class FakeUpdater extends EventEmitter {
  async checkForUpdates() { this.emit('checking-for-update'); this.emit('update-not-available') }
  quitAndInstall(...args) { this.installArgs = args }
}

test('checks releases and installs only after a download completes', async () => {
  const updater = new FakeUpdater()
  const events = []
  const controller = createUpdateController({ updater, enabled: true, version: '1.1.0', installDirectory: 'C:\\Monaw Install', publish: state => events.push(state) })
  assert.equal(updater.autoDownload, true)
  assert.equal(updater.autoInstallOnAppQuit, false)
  assert.equal(updater.allowPrerelease, false)
  assert.equal(updater.installDirectory, 'C:\\Monaw Install')
  assert.equal(controller.install(), false)
  assert.equal((await controller.check()).status, 'up-to-date')
  updater.emit('update-available', { version: '1.2.0' })
  updater.emit('download-progress', { percent: 46.7 })
  assert.equal(controller.getState().progress, 47)
  updater.emit('update-downloaded', { version: '1.2.0' })
  assert.equal(controller.getState().status, 'ready')
  assert.equal(controller.install(), true)
  assert.deepEqual(updater.installArgs, [true, true])
  assert.equal(events.at(-1).latestVersion, '1.2.0')
})

test('deduplicates concurrent checks and exposes a retryable error', async () => {
  const updater = new FakeUpdater()
  let calls = 0
  updater.checkForUpdates = async () => { calls++; throw new Error('private network detail') }
  const controller = createUpdateController({ updater, enabled: true, version: '1.1.0' })
  const results = await Promise.all([controller.check(), controller.check()])
  assert.equal(calls, 1)
  assert.equal(results[0].status, 'error')
  assert.doesNotMatch(results[0].error, /private network detail/)
  await controller.check()
  assert.equal(calls, 2)
})

test('development builds expose their version without checking or installing', async () => {
  const controller = createUpdateController({ updater: null, enabled: false, version: '1.1.0' })
  assert.deepEqual(await controller.check(), { currentVersion: '1.1.0', status: 'unavailable' })
  assert.equal(controller.install(), false)
})

test('a failed automatic download is handled and can be retried', async () => {
  const updater = new FakeUpdater()
  let rejectDownload
  updater.checkForUpdates = async () => {
    updater.emit('update-available', { version: '1.2.0' })
    return { downloadPromise: new Promise((_, reject) => { rejectDownload = reject }) }
  }
  const controller = createUpdateController({ updater, enabled: true, version: '1.1.0' })
  assert.equal((await controller.check()).status, 'downloading')
  rejectDownload(new Error('network interrupted'))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(controller.getState().status, 'error')
  assert.equal(controller.install(), false)
})
