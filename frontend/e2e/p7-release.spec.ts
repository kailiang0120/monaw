import { test, expect, type Desktop } from './desktop'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

async function runFor(desktop: Desktop, conversationId: string) {
  const route = `/api/runs?conversation_id=${encodeURIComponent(conversationId)}`
  await expect.poll(async () => (await desktop.api(route)).data.runs?.length ?? 0).toBeGreaterThan(0)
  const runs = (await desktop.api(route)).data.runs
  return runs.sort((left: { updated_at: string }, right: { updated_at: string }) => right.updated_at.localeCompare(left.updated_at))[0]
}

async function fileWriteRunFor(desktop: Desktop, conversationId: string) {
  const route = `/api/runs?conversation_id=${encodeURIComponent(conversationId)}`
  await expect.poll(async () => {
    const runs = (await desktop.api(route)).data.runs ?? []
    for (const run of runs) {
      const operations = (await desktop.api(`/api/runs/${run.run_id}/operations`)).data.operations ?? []
      if (operations.some((operation: { operation_kind: string }) => operation.operation_kind === 'file_write')) return true
    }
    return false
  }).toBe(true)
  const runs = (await desktop.api(route)).data.runs ?? []
  for (const run of runs) {
    const operations = (await desktop.api(`/api/runs/${run.run_id}/operations`)).data.operations ?? []
    if (operations.some((operation: { operation_kind: string }) => operation.operation_kind === 'file_write')) return run
  }
  throw new Error('No file write Run was created')
}

test('Continue resumes a clean pause after an opaque action without replaying it', async ({ desktop }) => {
  expect((await desktop.api('/api/settings', { permissions: { mode: 'full_access' } }, 'PUT')).status).toBe(200)
  await desktop.send('recover')
  await expect(desktop.page.getByText('Fixture recover completed.', { exact: false }).last()).toBeVisible()
  await desktop.send('opaque-resume')
  await desktop.started('opaque-resume')
  const conversation = (await desktop.api('/api/conversations')).data[0]
  const run = await runFor(desktop, conversation.id)
  const operations = async () => (await desktop.api(`/api/runs/${run.run_id}/operations`)).data.operations
  expect(await operations()).toEqual([expect.objectContaining({ effect_class: 'opaque_effect', status: 'returned' })])
  await desktop.page.getByRole('button', { name: 'Pause execution' }).click()
  await desktop.signal('opaque-resume.release')
  const continueButton = desktop.page.getByRole('button', { name: 'Continue execution' })
  await expect(continueButton).toBeVisible()
  expect((await desktop.api(`/api/runs/${run.run_id}`)).data).toMatchObject({
    status: 'paused', available_actions: expect.arrayContaining(['resume']),
  })
  await continueButton.click()
  await expect(desktop.page.getByText('Fixture opaque-resume completed.', { exact: false }).last()).toBeVisible()
  await expect.poll(async () => (await desktop.api(`/api/runs/${run.run_id}`)).data.status).toBe('completed')
  expect(await operations()).toHaveLength(1)
  expect((await desktop.events()).filter((event) => event.kind === 'model' && event.scenario === 'opaque-resume')
    .map((event) => event.index)).toEqual([0, 1, 2])
  expect(desktop.errors).toEqual([])
})

test('file diff, approval, safe pause, real app/backend restart, Continue and Undo', async ({ desktop }) => {
  test.setTimeout(150_000)
  await desktop.send('recover')
  await expect(desktop.page.getByText('Fixture recover completed.', { exact: false }).last()).toBeVisible()
  const target = join(desktop.root, 'workspace', 'file-resume.txt')
  await writeFile(target, 'before approval\n')

  await desktop.send('file-resume')
  const preview = desktop.page.getByRole('region', { name: 'File change preview' })
  await expect(preview).toBeVisible()
  await expect(preview.getByLabel('Text diff')).toContainText('-before approval')
  await expect(preview.getByLabel('Text diff')).toContainText('+approved update')
  expect(await readFile(target, 'utf8')).toBe('before approval\n')

  const conversations = (await desktop.api('/api/conversations')).data
  expect(conversations).toHaveLength(1)
  const conversationId = conversations[0].id as string
  const run = await runFor(desktop, conversationId)
  const pausePosts: string[] = []
  desktop.page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === `/api/runs/${run.run_id}/pause`) {
      pausePosts.push(request.url())
    }
  })

  const approve = desktop.page.getByRole('button', { name: 'Approve', exact: true })
  await approve.focus()
  await desktop.page.keyboard.press('Enter')
  await desktop.started('file-resume')
  expect(await readFile(target, 'utf8')).toBe('approved update\n')
  await expect.poll(async () => {
    const operations = (await desktop.api(`/api/runs/${run.run_id}/operations`)).data.operations
    return operations?.map((operation: { operation_kind: string; status: string }) => `${operation.operation_kind}:${operation.status}`)
  }).toEqual(['file_write:verified'])

  const pause = desktop.page.getByRole('button', { name: 'Pause execution' })
  const pauseResponse = desktop.page.waitForResponse((response) => response.url().endsWith(`/api/runs/${run.run_id}/pause`))
  await pause.focus()
  await desktop.page.keyboard.press('Enter')
  await expect.poll(() => pausePosts.length).toBe(1)
  expect((await pauseResponse).status()).toBe(202)
  await desktop.signal('file-resume.release')
  await expect.poll(async () => (await desktop.api(`/api/runs/${run.run_id}`)).data.status).toBe('paused')
  const paused = (await desktop.api(`/api/runs/${run.run_id}`)).data
  expect(paused.available_actions).toContain('resume')
  expect(pausePosts).toHaveLength(1)
  expect((await desktop.api(`/api/runs/${run.run_id}/recovery`)).data.run.status).toBe('paused')

  const oldElectronPid = desktop.app.process().pid
  const backendsBefore = (await desktop.events()).filter((event) => event.kind === 'backend_started')
  await desktop.close()
  await desktop.start()
  expect(desktop.app.process().pid).not.toBe(oldElectronPid)
  const backendsAfter = (await desktop.events()).filter((event) => event.kind === 'backend_started')
  expect(backendsAfter.length).toBeGreaterThan(backendsBefore.length)
  expect(backendsAfter.at(-1).pid).not.toBe(backendsBefore.at(-1).pid)

  await desktop.page.getByText('[e2e:recover]', { exact: true }).last().click()
  await expect(desktop.page.getByText('[e2e:file-resume]', { exact: true }).last()).toBeVisible()
  const recoveredReport = (await desktop.api(`/api/runs/${run.run_id}/recovery`)).data
  expect(recoveredReport.run).toMatchObject({ status: 'paused', available_actions: expect.arrayContaining(['resume']) })
  expect(recoveredReport.unresolved_operations).toEqual([])
  await expect(desktop.page.getByRole('button', { name: 'Continue execution' })).toBeVisible()

  const continuePosts: string[] = []
  const followRequests: string[] = []
  desktop.page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && path === `/api/runs/${run.run_id}/resume`) {
      continuePosts.push(request.url())
    }
    // Continue shows the resumed work live instead of waiting for the Run to end.
    if (request.method() === 'GET' && path === `/api/chat/runs/${run.run_id}/events`) {
      followRequests.push(request.url())
    }
  })
  await desktop.signal('file-resume-continue.hold')
  const continueButton = desktop.page.getByRole('button', { name: 'Continue execution' })
  await continueButton.focus()
  await desktop.page.keyboard.press('Enter')
  await desktop.page.keyboard.press('Enter')
  await expect.poll(() => continuePosts.length).toBe(1)
  // While the resumed model call is held, the chat is following it: Stop and
  // Pause are offered for that Run, not a stale "Paused" bubble.
  await desktop.started('file-resume-continue')
  await expect.poll(() => followRequests.length).toBeGreaterThan(0)
  await expect(desktop.page.getByRole('button', { name: 'Stop response' })).toBeVisible()
  await expect(desktop.page.getByRole('button', { name: 'Pause execution' })).toBeVisible()
  await desktop.signal('file-resume-continue.release')
  await expect.poll(async () => (await desktop.api(`/api/runs/${run.run_id}`)).data.status).toBe('completed')
  await expect.poll(async () => (await desktop.api(`/api/runs/${run.run_id}`)).data.attempt).toBe(2)
  await expect(desktop.page.getByText('Fixture file-resume completed.', { exact: false }).last()).toBeVisible()
  // The reply that wrote the file lists it in its Changes summary.
  await expect(desktop.page.getByRole('region', { name: 'Changes in this reply' }).first()).toContainText('file-resume.txt')
  const operations = (await desktop.api(`/api/runs/${run.run_id}/operations`)).data.operations
  expect(operations.filter((operation: { operation_kind: string }) => operation.operation_kind === 'file_write')).toHaveLength(1)
  expect((await desktop.events()).filter((event) => event.kind === 'model' && event.scenario === 'file-resume').map((event) => event.index)).toEqual([0, 1, 2])
  expect((await desktop.api('/api/settings')).data.permissions.mode).toBe('default')

  await desktop.page.getByRole('button', { name: 'Changes', exact: true }).click()
  const reviewUndo = desktop.page.getByRole('button', { name: 'Undo', exact: true })
  await expect(reviewUndo).toBeVisible()
  await reviewUndo.focus()
  await desktop.page.keyboard.press('Enter')
  const undoPreview = desktop.page.getByRole('region', { name: 'Confirm Git Undo' })
  await expect(undoPreview).toBeVisible()
  await expect(undoPreview).toContainText('file-resume.txt')
  await expect(undoPreview.locator('pre')).toContainText('-approved update')
  await expect(undoPreview.locator('pre')).toContainText('+before approval')
  // Focus Cancel, then Shift+Tab selects the destructive
  // action explicitly, then Enter exercises the keyboard path through Undo.
  await undoPreview.getByRole('button', { name: 'Cancel', exact: true }).focus()
  await desktop.page.keyboard.press('Shift+Tab')
  await desktop.page.keyboard.press('Enter')
  await expect.poll(() => readFile(target, 'utf8')).toBe('before approval\n')
  await expect(desktop.page.getByText('Undone', { exact: true })).toBeVisible()
  expect(continuePosts).toHaveLength(1)
  expect(desktop.errors).toEqual([])
})

test('hard backend crash after a verified write exposes Continue without duplicating the user message or write', async ({ desktop }) => {
  test.setTimeout(150_000)
  await desktop.send('recover')
  await expect(desktop.page.getByText('Fixture recover completed.', { exact: false }).last()).toBeVisible()
  const target = join(desktop.root, 'workspace', 'file-resume.txt')
  await writeFile(target, 'before hard crash\n')

  await desktop.send('file-resume')
  const preview = desktop.page.getByRole('region', { name: 'File change preview' })
  await expect(preview).toBeVisible()
  await expect(preview.getByLabel('Text diff')).toContainText('+approved update')
  expect(await readFile(target, 'utf8')).toBe('before hard crash\n')
  const conversationId = (await desktop.api('/api/conversations')).data[0].id as string
  const run = await runFor(desktop, conversationId)

  await desktop.page.getByRole('button', { name: 'Approve', exact: true }).click()
  await desktop.started('file-resume')
  await expect.poll(async () => {
    const operations = (await desktop.api(`/api/runs/${run.run_id}/operations`)).data.operations ?? []
    return operations.map((operation: { operation_kind: string; status: string }) => `${operation.operation_kind}:${operation.status}`)
  }).toEqual(['file_write:verified'])
  expect(await readFile(target, 'utf8')).toBe('approved update\n')

  const backendStartsBeforeCrash = (await desktop.events()).filter((event) => event.kind === 'backend_started').length
  await desktop.signal('restart.request')
  await expect.poll(async () =>
    (await desktop.events()).filter((event) => event.kind === 'backend_started').length,
    { timeout: 45_000 },
  ).toBeGreaterThan(backendStartsBeforeCrash)
  // Python startup is recorded before the new HTTP listener is ready.
  await expect.poll(async () => desktop.api(`/api/runs/${run.run_id}`)
    .then(({ data }) => data.status, () => 'starting')).toBe('paused')
  const recovered = (await desktop.api(`/api/runs/${run.run_id}`)).data
  expect(recovered.available_actions).toContain('resume')

  await desktop.close()
  await desktop.start()
  await desktop.page.getByText('[e2e:recover]', { exact: true }).last().click()
  await expect(desktop.page.getByText('[e2e:file-resume]', { exact: true }).last()).toBeVisible()
  await expect(desktop.page.getByRole('button', { name: 'Continue execution' })).toBeVisible()
  await desktop.page.getByRole('button', { name: 'Continue execution' }).click()
  await expect.poll(async () => (await desktop.api(`/api/runs/${run.run_id}`)).data.status).toBe('completed')
  await expect.poll(async () => (await desktop.api(`/api/runs/${run.run_id}`)).data.attempt).toBe(2)
  await expect(desktop.page.getByText('Fixture file-resume completed.', { exact: false }).last()).toBeVisible()

  expect(await readFile(target, 'utf8')).toBe('approved update\n')
  const operations = (await desktop.api(`/api/runs/${run.run_id}/operations`)).data.operations
  expect(operations.filter((operation: { operation_kind: string }) => operation.operation_kind === 'file_write')).toHaveLength(1)
  const messages = (await desktop.api(`/api/conversations/${conversationId}/messages`)).data.messages
  expect(messages.filter((message: { role: string; content: string }) =>
    message.role === 'user' && message.content === '[e2e:file-resume]')).toHaveLength(1)
  expect((await desktop.events()).filter((event) => event.kind === 'model' && event.scenario === 'file-resume').map((event) => event.index)).toEqual([0, 1, 2])
  expect(desktop.errors).toEqual([])
})

test('Undo rechecks the file and preserves external edits on conflict', async ({ desktop }, testInfo) => {
  await desktop.send('recover')
  await expect(desktop.page.getByText('Fixture recover completed.', { exact: false }).last()).toBeVisible()
  const target = join(desktop.root, 'workspace', 'file-conflict.txt')
  await writeFile(target, 'original conflict target\n')
  await desktop.send('file-conflict')
  const preview = desktop.page.getByRole('region', { name: 'File change preview' })
  await expect(preview.getByLabel('Text diff')).toContainText('+approved update')
  expect(await readFile(target, 'utf8')).toBe('original conflict target\n')
  await desktop.page.getByRole('button', { name: 'Approve', exact: true }).click()
  await expect(desktop.page.getByText('Fixture file-conflict completed.', { exact: false }).last()).toBeVisible()

  const conversation = (await desktop.api('/api/conversations')).data[0]
  const run = await fileWriteRunFor(desktop, conversation.id)
  await expect.poll(async () => (await desktop.api(`/api/runs/${run.run_id}`)).data.status).toBe('completed')
  const operation = (await desktop.api(`/api/runs/${run.run_id}/operations`)).data.operations
    .find((item: { operation_kind: string }) => item.operation_kind === 'file_write')
  expect(operation).toBeTruthy()
  expect(operation).toMatchObject({ status: 'verified', effect_class: 'verifiable_write', snapshot_available: true })
  expect((await desktop.api('/api/settings')).data.permissions.mode).toBe('default')
  const capabilityPath = `/api/file-mutations/${encodeURIComponent(operation.operation_id)}/undo-capability?run_id=${encodeURIComponent(run.run_id)}&conversation_id=${encodeURIComponent(conversation.id)}`
  const capability = (await desktop.api(capabilityPath)).data
  expect(capability.reason_code, JSON.stringify(capability)).toBeNull()
  expect(capability.status).toBe('available')

  // A real restart retains the default permission profile and rechecks Undo.
  await desktop.close()
  await desktop.start()
  await desktop.page.getByText('[e2e:recover]', { exact: true }).last().click()
  expect((await desktop.api('/api/settings')).data.permissions.mode).toBe('default')
  await desktop.page.getByRole('button', { name: 'Changes', exact: true }).click()
  const reviewUndo = desktop.page.getByRole('button', { name: 'Undo', exact: true })
  await expect(reviewUndo).toBeVisible()
  await reviewUndo.click()
  const undoPreview = desktop.page.getByRole('region', { name: 'Confirm Git Undo' })
  await expect(undoPreview).toBeVisible()

  for (const theme of ['light', 'dark']) {
    await desktop.page.getByRole('button', { name: 'Back to chat', exact: true }).click()
    const toggle = desktop.page.getByRole('button', { name: `Switch to ${theme} mode`, exact: true }).last()
    if (await toggle.isVisible()) await toggle.click()
    await desktop.page.screenshot({ path: testInfo.outputPath(`chat-${theme}.png`) })
    await desktop.page.getByRole('button', { name: 'Changes', exact: true }).click()
    await desktop.page.getByRole('button', { name: 'Undo', exact: true }).click()
    await expect(undoPreview).toBeVisible()
    await desktop.page.getByRole('dialog', { name: 'Workspace changes' })
      .screenshot({ path: testInfo.outputPath(`undo-${theme}.png`) })
  }

  await writeFile(target, 'external edit must stay\n')
  await undoPreview.getByRole('button', { name: 'Undo', exact: true }).click()
  await expect(desktop.page.getByRole('alert').filter({ hasText: 'Undo is unavailable: the file changed or needs review.' })).toBeVisible()
  expect(await readFile(target, 'utf8')).toBe('external edit must stay\n')
  await expect.poll(async () => (await desktop.api(capabilityPath)).data.status).toBe('conflict')
  expect(desktop.errors).toEqual([])
})
