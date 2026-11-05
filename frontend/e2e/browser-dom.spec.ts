import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { test, expect } from './desktop'

test('DOM inspection reads form errors and waits observe dynamic controls without side effects', async ({ desktop }) => {
  const source = await readFile(resolve(import.meta.dirname, '../../backend/app/skills/browser_use/extension/dom_inspection.py'), 'utf8')
  const declaration = source.match(/READ_ELEMENT = r"""([\s\S]*?)"""/)?.[1]
  if (!declaration) throw new Error('Missing production DOM inspection function')
  const actionDeclaration = source.match(/ACTION_TARGET = r"""([\s\S]*?)"""/)?.[1]?.replace('__READ_ELEMENT__', declaration)
  if (!actionDeclaration) throw new Error('Missing production action readiness function')
  const { page } = desktop
  await page.setContent(`
    <form><label for="email">Email address</label>
      <input id="email" type="email" required aria-describedby="hint" aria-errormessage="error" aria-invalid="true">
      <p id="hint">Use your work email.</p><p id="error">Email is required.</p>
      <input id="password" type="password" value="fixture-private-password">
      <input id="token" type="hidden" value="fixture-private-token">
      <button id="submit" disabled>Submit</button>
      <input id="custom" type="checkbox" style="width:0;height:0;opacity:0"><label for="custom">Custom checkbox</label>
      <textarea id="readonly" aria-readonly="true">Locked</textarea>
    </form><div id="loading" style="position:fixed;inset:0;background:white;z-index:10">Loading</div>
  `)
  const read = (selector: string, details = true) => page.evaluate<Record<string, unknown>>(
    `(${declaration}).call(globalThis, ${JSON.stringify(selector)}, ${details})`,
  )
  const actionState = (selector: string, action = 'click') => page.evaluate<Record<string, unknown>>(
    `(${actionDeclaration}).call(document.querySelector(${JSON.stringify(selector)}), ${JSON.stringify(action)})`,
  )
  await page.evaluate(() => {
    const root = document.documentElement
    root.dataset.inspectionMutations = '0'
    root.dataset.inspectionEvents = '0'
    new MutationObserver((records) => {
      if (records.some(record => !(record.target === root && record.attributeName?.startsWith('data-inspection')))) {
        root.dataset.inspectionMutations = String(Number(root.dataset.inspectionMutations) + 1)
      }
    }).observe(document.body, { attributes: true, childList: true, subtree: true })
    for (const name of ['input', 'change', 'invalid', 'focus', 'click']) {
      document.addEventListener(name, () => { root.dataset.inspectionEvents = String(Number(root.dataset.inspectionEvents) + 1) }, true)
    }
  })
  expect(await read('#email')).toMatchObject({
    tag: 'input', attributes: { id: 'email', type: 'email' }, labels: ['Email address'],
    description: ['Use your work email.'], validation: { invalid: true, errors: ['Email is required.'] },
    visible: true, covered: true, ready: false, coveringElement: { id: 'loading' },
  })
  expect(await read('#password')).toMatchObject({ value: '[redacted]' })
  expect(JSON.stringify(await read('#token'))).not.toContain('fixture-private-token')
  expect(await read('input')).toHaveProperty('error')
  expect(await read('[')).toHaveProperty('error')
  expect(await page.evaluate(() => ({ ...document.documentElement.dataset }))).toMatchObject({
    inspectionMutations: '0', inspectionEvents: '0',
  })
  expect(await read('#submit', false)).toMatchObject({ enabled: false, ready: false })
  expect(await actionState('#submit')).toMatchObject({ enabled: false, ready: false, covered: true })
  expect(await actionState('#custom')).toMatchObject({ visible: true, covered: true, ready: false })
  // A SPA can replace a disabled control and remove its overlay without navigating.
  await page.evaluate(() => {
    setTimeout(() => {
      document.querySelector('#loading')?.remove()
      document.querySelector('#submit')!.outerHTML = '<button id="submit">Submit</button>'
      const email = document.querySelector<HTMLInputElement>('#email')!
      email.value = 'person@example.com'
      email.removeAttribute('aria-invalid')
    }, 150)
  })
  await expect.poll(async () => (await read('#loading', false)).attached).toBe(false)
  await expect.poll(async () => (await read('#submit', false)).ready).toBe(true)
  expect(await read('#email')).toMatchObject({ editable: true, value: 'person@example.com', validation: { invalid: false } })
  expect(await actionState('#submit')).toMatchObject({ enabled: true, ready: true })
  expect(await actionState('#custom')).toMatchObject({ visible: true, ready: true })
  expect(await actionState('#readonly', 'fill')).toMatchObject({ readonly: true, ready: false })
  expect(await read('#readonly', false)).toMatchObject({ editable: false })
  expect(await page.evaluate(() => document.documentElement.dataset.inspectionEvents)).toBe('0')
})
