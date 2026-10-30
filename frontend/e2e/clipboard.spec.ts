import { test, expect } from './desktop'
import { join } from 'node:path'

test('extension folder Copy writes the OS clipboard from the sandboxed renderer', async ({ desktop }) => {
  const marker = 'monaw-clipboard-regression-marker'
  const folder = join(desktop.root, 'browser', 'extension')
  const previous = await desktop.app.evaluate(({ clipboard }, marker) => {
    const previous = clipboard.readText()
    clipboard.writeText(marker)
    return previous
  }, marker)
  try {
    // Use real authenticated setup inside the disposable home. The Python
    // fixture suppresses Explorer/Chrome launch; browser settings stay untouched.
    await desktop.page.getByRole('button', { name: 'Settings', exact: true }).click()
    await desktop.page.getByRole('button', { name: 'Browser', exact: true }).click()
    const installed = desktop.page.waitForResponse(response => response.url().endsWith('/extension/install') && response.request().method() === 'POST')
    await desktop.page.getByRole('button', { name: 'Set up extension', exact: true }).click()
    expect((await installed).status()).toBe(200)
    await expect(desktop.page.getByRole('textbox', { name: 'Extension folder' })).toHaveValue(folder)
    await desktop.page.getByRole('button', { name: 'Copy', exact: true }).click()
    await expect(desktop.page.getByRole('button', { name: 'Copied', exact: true })).toBeVisible()
    expect(await desktop.app.evaluate(({ clipboard }) => clipboard.readText())).toBe(folder)
    expect(await desktop.page.evaluate(async () => {
      try { await window.electronAPI!.writeClipboardText!('x'.repeat(20001)); return 'accepted' }
      catch { return 'rejected' }
    })).toBe('rejected')
    expect(await desktop.app.evaluate(({ clipboard }) => clipboard.readText())).toBe(folder)
  } finally {
    // Avoid retaining fixture text, and don't overwrite a user's newer copy.
    await desktop.app.evaluate(({ clipboard }, { previous, folder, marker }) => {
      if ([folder, marker].includes(clipboard.readText())) clipboard.writeText(previous)
    }, { previous, folder, marker })
  }
})
