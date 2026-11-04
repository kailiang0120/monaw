import { test, expect } from './desktop'

test('execution UI reads the real Run routes and accepts a keyboard Stop once', async ({ desktop }) => {
  // Create and finish a first turn so the next live Run belongs to an already
  // selected conversation, which exercises the renderer's route wiring.
  await desktop.send('recover')
  await expect(desktop.page.getByText('Fixture recover completed.', { exact: false }).last()).toBeVisible()
  const conversation = (await desktop.api('/api/conversations')).data[0]

  await desktop.send('stream')
  await desktop.started('stream')

  const route = `/api/runs?conversation_id=${encodeURIComponent(conversation.id)}`
  await expect.poll(async () => {
    const page = (await desktop.api(route)).data
    return page.runs?.some((run: { status: string }) => run.status === 'running') ?? false
  }).toBe(true)
  const runPage = (await desktop.api(route)).data
  const activeRun = runPage.runs.find((run: { status: string }) => run.status === 'running')
  expect(activeRun).toMatchObject({
    conversation_id: conversation.id,
    attempt: 1,
    status: 'running',
    available_actions: expect.arrayContaining(['stop']),
  })

  const eventPage = (await desktop.api(`/api/runs/${activeRun.run_id}/events?after_seq=0&limit=20`)).data
  expect(eventPage).toMatchObject({ run_id: activeRun.run_id, events: expect.any(Array) })
  expect(eventPage.events[0]).toMatchObject({
    run_id: activeRun.run_id,
    seq: 1,
    type: 'run.created',
  })
  await expect(desktop.page.getByRole('status').filter({ hasText: 'Running' }).first()).toBeVisible()

  const stopPosts: string[] = []
  desktop.page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === `/api/runs/${activeRun.run_id}/stop`) {
      stopPosts.push(request.url())
    }
  })
  const stop = desktop.page.getByRole('button', { name: 'Stop execution' })
  await stop.focus()
  await desktop.page.keyboard.press('Enter')
  await expect.poll(() => stopPosts.length).toBe(1)
  await expect.poll(async () => (await desktop.api(`/api/runs/${activeRun.run_id}`)).data.status)
    .toMatch(/^(cancelled|interrupted)$/)
  expect(stopPosts).toHaveLength(1)
  await expect(desktop.page.getByRole('button', { name: 'Stop execution' })).toHaveCount(0)

  await desktop.signal('stream.release')
  expect(desktop.errors).toEqual([])
})
