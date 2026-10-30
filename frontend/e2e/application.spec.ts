import { test, expect } from './desktop'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

async function toolRows(desktop: import('./desktop').Desktop) {
  const conversations = (await desktop.api('/api/conversations')).data
  const messages = (await desktop.api(`/api/conversations/${conversations[0].id}/messages`)).data.messages
  return messages.flatMap((message: { tool_calls?: unknown[] }) => message.tool_calls || [])
}

test('authenticated startup, streamed response, and persistence after reopening', async ({ desktop }) => {
  const { page, port } = desktop
  expect((await page.request.get(`http://127.0.0.1:${port}/api/settings`)).status()).toBe(401)
  const windows = await desktop.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)
  expect(windows).toBe(1)
  expect(await page.evaluate(() => ({
    require: typeof (window as unknown as { require?: unknown }).require,
    process: typeof (window as unknown as { process?: unknown }).process,
    bridge: window.electronAPI?.isElectron,
  }))).toEqual({ require: 'undefined', process: 'undefined', bridge: true })
  expect(await page.evaluate(async () => new Promise<boolean>((done) => {
    const blocked = 'http://127.0.0.1:1/not-monaw'
    const timer = setTimeout(() => { document.removeEventListener('securitypolicyviolation', handler); done(false) }, 1500)
    function handler(event: SecurityPolicyViolationEvent) {
      if (event.effectiveDirective !== 'connect-src' || event.blockedURI !== blocked) return
      clearTimeout(timer)
      document.removeEventListener('securitypolicyviolation', handler)
      done(true)
    }
    document.addEventListener('securitypolicyviolation', handler)
    void fetch(blocked).catch(() => {})
  }))).toBe(true)
  const response = page.waitForResponse((r) => r.url().endsWith('/api/chat') && r.request().method() === 'POST')
  await desktop.send('stream')
  await desktop.started('stream')
  await expect(page.getByRole('button', { name: 'Stop response', exact: true })).toBeVisible()
  await desktop.signal('stream.release')
  const events = await (await response).text()
  expect((events.match(/event: token\n/g) || []).length).toBeGreaterThan(1)
  expect(events.indexOf('event: token')).toBeLessThan(events.indexOf('event: done'))
  await expect(page.getByText('Fixture stream completed.', { exact: false }).last()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stop response', exact: true })).toHaveCount(0)
  const conversations = (await desktop.api('/api/conversations')).data
  expect(conversations).toHaveLength(1)
  const messages = (await desktop.api(`/api/conversations/${conversations[0].id}/messages`)).data.messages
  expect(messages.some((m: { role: string; content: string }) => m.role === 'assistant' && m.content.includes('Fixture stream completed.'))).toBe(true)
  await desktop.close()
  await desktop.start()
  await desktop.page.getByText('[e2e:stream]', { exact: true }).last().click()
  await expect(desktop.page.getByText('Fixture stream completed.', { exact: false }).last()).toBeVisible()
  expect(desktop.errors).toEqual([])
})

test('approved file action runs once through the real execution gate', async ({ desktop }) => {
  await desktop.send('allow')
  await expect(desktop.page.getByRole('button', { name: 'Approve', exact: true })).toBeVisible()
  expect(await readFile(join(desktop.root, 'workspace', 'allow.txt'), 'utf8').catch(() => null)).toBeNull()
  await desktop.page.getByRole('button', { name: 'Approve', exact: true }).click()
  await expect(desktop.page.getByText('Fixture allow completed.', { exact: false }).last()).toBeVisible()
  expect(await readFile(join(desktop.root, 'workspace', 'allow.txt'), 'utf8')).toBe('one change\n')
  const rows = await toolRows(desktop)
  expect(rows.filter((row: { tool_name: string; status: string }) => row.tool_name === 'fs_append' && row.status === 'complete')).toHaveLength(1)
})

test('rejected file action is visible and leaves the disposable file unchanged', async ({ desktop }) => {
  const target = join(desktop.root, 'workspace', 'deny.txt')
  await writeFile(target, 'original\n')
  await desktop.send('deny')
  await desktop.page.getByRole('button', { name: 'Reject', exact: true }).click()
  await expect(desktop.page.getByText('Rejected', { exact: true })).toBeVisible()
  await expect(desktop.page.getByText('Fixture deny completed.', { exact: false }).last()).toBeVisible()
  expect(await readFile(target, 'utf8')).toBe('original\n')
  const rows = await toolRows(desktop)
  expect(rows.some((row: { tool_name: string; status: string }) => row.tool_name === 'fs_append' && row.status !== 'complete')).toBe(true)
})

test('stop retains a completed sibling and cancels unfinished parallel work', async ({ desktop }) => {
  await desktop.send('parallel')
  await desktop.started('parallel')
  await desktop.page.getByRole('button', { name: 'Stop response', exact: true }).click()
  await expect(desktop.page.getByRole('button', { name: 'Stop response', exact: true })).toHaveCount(0)
  await expect.poll(async () => {
    const rows = await toolRows(desktop)
    return rows.map((row: { tool_name: string; status: string }) => `${row.tool_name}:${row.status}`).sort()
  }).toEqual(['e2e_barrier:cancelled', 'fs_read:complete'])
  const rows = await toolRows(desktop)
  expect(rows.find((row: { tool_name: string }) => row.tool_name === 'fs_read').output).toContain('Completed sibling evidence')
})

test('schedule created in the UI stays disabled after active completion and application restart', async ({ desktop }) => {
  const title = 'Disposable recurring task'
  await desktop.page.getByRole('button', { name: 'Create scheduled task', exact: true }).click()
  const dialog = desktop.page.getByRole('dialog', { name: 'New scheduled task' })
  await dialog.getByRole('textbox', { name: 'Title', exact: true }).fill(title)
  await dialog.getByRole('textbox', { name: 'Prompt', exact: true }).fill('[e2e:schedule]')
  await dialog.getByRole('button', { name: 'Every so often', exact: true }).click()
  await dialog.getByRole('spinbutton', { name: 'Interval value' }).fill('1')
  await dialog.getByRole('button', { name: 'Create task', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  const task = (await desktop.api('/api/scheduled-tasks')).data[0]
  await desktop.page.getByRole('button', { name: `Scheduled task actions for ${title}` }).click()
  await desktop.page.getByRole('menuitem', { name: 'Run now', exact: true }).click()
  await desktop.started('schedule')
  await desktop.page.getByRole('button', { name: `Scheduled task actions for ${title}` }).click()
  await desktop.page.getByRole('menuitem', { name: 'Disable', exact: true }).click()
  await expect.poll(async () => (await desktop.api('/api/scheduled-tasks')).data[0].enabled).toBe(false)
  await desktop.signal('schedule.release')
  await expect.poll(async () => (await desktop.api(`/api/scheduled-tasks/${task.id}/runs`)).data[0]?.status).toBe('succeeded')
  await expect(desktop.page.getByText('Fixture schedule completed.', { exact: false }).last()).toBeVisible()
  await desktop.close()
  await writeFile(join(desktop.root, 'control', 'clock-offset'), '180')
  await desktop.start()
  const ticks = Number(await readFile(join(desktop.root, 'control', 'scheduler-ticks'), 'utf8'))
  await expect.poll(async () => Number(await readFile(join(desktop.root, 'control', 'scheduler-ticks'), 'utf8'))).toBeGreaterThan(ticks + 2)
  const saved = (await desktop.api('/api/scheduled-tasks')).data[0]
  expect(saved.enabled).toBe(false)
  expect(saved.nextRunAt).toBe('')
  expect((await desktop.api(`/api/scheduled-tasks/${task.id}/runs`)).data).toHaveLength(1)
  expect((await desktop.events()).filter((event) => event.kind === 'model' && event.scenario === 'schedule')).toHaveLength(1)
})

test('a lost run-now response reconciles in the UI without accepting a second run', async ({ desktop }) => {
  const title = 'Lost response task'
  const created = await desktop.api('/api/scheduled-tasks', {
    title, prompt: '[e2e:schedule]', scheduleKind: 'interval', intervalSeconds: 3600, timezone: 'UTC',
  })
  expect(created.status).toBe(200)
  const task = created.data
  let posts = 0
  await desktop.page.route(`**/api/scheduled-tasks/${task.id}/run`, async (route) => {
    posts++
    const accepted = await route.fetch()
    expect(accepted.status()).toBe(200)
    await route.abort('failed')
  })
  await desktop.page.getByRole('button', { name: `Scheduled task actions for ${title}` }).click()
  await desktop.page.getByRole('menuitem', { name: 'Run now', exact: true }).click()
  await desktop.started('schedule')
  await desktop.page.getByRole('button', { name: 'Check run', exact: true }).click()
  await expect(desktop.page.getByRole('button', { name: 'Check run', exact: true })).toHaveCount(0)
  expect(posts).toBe(1)
  const runs = (await desktop.api(`/api/scheduled-tasks/${task.id}/runs`)).data
  expect(runs).toHaveLength(1)
  const conversations = (await desktop.api('/api/conversations')).data
  expect(conversations).toHaveLength(1)
  expect(runs[0].conversationId).toBe(conversations[0].id)
  await desktop.signal('schedule.release')
  await expect.poll(async () => (await desktop.api(`/api/scheduled-tasks/${task.id}/runs`)).data[0]?.status).toBe('succeeded')
  await expect(desktop.page.getByText('Fixture schedule completed.', { exact: false }).last()).toBeVisible()
  expect((await desktop.events()).filter((event) => event.kind === 'model' && event.scenario === 'schedule')).toHaveLength(1)
})

test('backend interruption is visible and recovery does not repeat a completed file action', async ({ desktop }) => {
  expect((await desktop.api('/api/settings', { permissions: { mode: 'full_access' } }, 'PUT')).status).toBe(200)
  await desktop.send('interrupt')
  await desktop.started('interrupt')
  const target = join(desktop.root, 'workspace', 'interrupt.txt')
  expect(await readFile(target, 'utf8')).toBe('one change\n')
  await desktop.signal('restart.request')
  await expect(desktop.page.getByText('Stopped with an error', { exact: true })).toBeVisible()
  await expect.poll(async () => desktop.api('/api/settings').then((r) => r.status, () => 0)).toBe(200)
  await desktop.send('recover')
  await expect(desktop.page.getByText('Fixture recover completed.', { exact: false }).last()).toBeVisible()
  expect(await readFile(target, 'utf8')).toBe('one change\n')
  const events = await desktop.events()
  expect(events.filter((event) => event.kind === 'restart')).toHaveLength(1)
  expect(events.filter((event) => event.kind === 'model' && event.scenario === 'interrupt')).toHaveLength(2)
})
