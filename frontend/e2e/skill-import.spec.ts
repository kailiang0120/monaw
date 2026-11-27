import { test, expect } from './desktop'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

// Import a real portable skill into a disposable profile and verify runtime discovery.
test('skills imported through Settings persist and become available without restarting', async ({ desktop }, testInfo) => {
  const folder = join(desktop.root, 'control', 'import-demo')
  await mkdir(join(folder, 'references'), { recursive: true })
  await writeFile(join(folder, 'SKILL.md'), '---\nname: import-demo\ndescription: Summarize project notes.\n---\nRead references/guide.md before writing.\n')
  await writeFile(join(folder, 'references', 'guide.md'), 'Keep the summary concise.\n')
  await desktop.app.evaluate(({ dialog }, path) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path] })
  }, folder)
  const { page } = desktop
  await page.getByRole('button', { name: 'Settings', exact: true }).click()
  const settings = page.getByRole('dialog', { name: 'Settings', exact: true })
  await settings.locator('nav').getByRole('button', { name: 'Tools', exact: true }).click()
  await settings.getByRole('button', { name: 'Import', exact: true }).click()
  const importCard = settings.locator('.st-card').filter({ has: page.getByRole('heading', { name: 'Import skills', exact: true }) })
  for (const theme of ['light', 'dark']) {
    // Inspect the native picker and form layout in each theme.
    await page.evaluate((theme) => {
      const root = document.querySelector('.app-surface')!
      root.classList.remove('theme-light', 'theme-dark')
      root.classList.add(`theme-${theme}`)
    }, theme)
    await importCard.screenshot({ path: testInfo.outputPath(`import-form-${theme}.png`) })
  }
  await settings.getByRole('button', { name: 'Browse', exact: true }).click()
  await expect(settings.getByRole('textbox', { name: 'GitHub URL or local folder' })).toHaveValue(folder)
  await settings.getByRole('button', { name: 'Check source' }).click()
  await expect(settings.getByRole('checkbox', { name: /import-demo/ })).toBeChecked()
  for (const theme of ['light', 'dark']) {
    // Match both themes without touching the user's real profile.
    await page.evaluate((theme) => {
      const root = document.querySelector('.app-surface')!
      root.classList.remove('theme-light', 'theme-dark')
      root.classList.add(`theme-${theme}`)
    }, theme)
    await importCard.screenshot({ path: testInfo.outputPath(`import-preview-${theme}.png`) })
  }
  await settings.getByRole('button', { name: 'Install 1 skill', exact: true }).click()
  await expect(settings.getByRole('status').filter({ hasText: 'Installed import-demo' })).toBeVisible()
  const fresh = (await desktop.api('/api/settings')).data
  expect(fresh.tools.skills['import-demo']).toBe(true)
  expect(fresh.available_skills.find((skill: { name: string }) => skill.name === 'import-demo').enabled).toBe(true)
  expect(await readFile(join(desktop.root, 'skills', 'import-demo', 'references', 'guide.md'), 'utf8')).toContain('concise')
  await settings.locator('nav').getByRole('button', { name: 'Sandbox', exact: true }).click()
  for (const label of ['Container network', 'Container files']) {
    await settings.getByRole('button', { name: `About ${label}`, exact: true }).click()
    await expect(page.getByRole('tooltip')).toBeVisible()
    await page.keyboard.press('Escape')
  }
  await settings.getByRole('button', { name: 'Close settings', exact: true }).click()
  await desktop.close()
  await desktop.start()
  expect((await desktop.api('/api/settings')).data.tools.skills['import-demo']).toBe(true)
})
