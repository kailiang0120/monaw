import { test, expect } from './desktop'

// Verify the built renderer's catalog and offline assets in both themes.
test('connector catalog groups apps and renders offline logos in both themes', async ({
  desktop,
}, testInfo) => {
  const { page } = desktop
  const externalImages: string[] = []
  page.on('request', (request) => {
    if (request.resourceType() === 'image' && /^https:\/\//.test(request.url()))
      externalImages.push(request.url())
  })
  await page.getByRole('button', { name: 'Settings', exact: true }).click()
  const settings = page.getByRole('dialog', { name: 'Settings', exact: true })
  await settings.locator('nav').getByRole('button', { name: 'Tools', exact: true }).click()
  const catalog = settings.getByRole('tabpanel', { name: 'Connectors', exact: true })
  await expect(catalog.locator('.st-connector-card')).toHaveCount(11)
  await expect(catalog.locator('.st-connector-card-unavailable')).toHaveCount(3)
  await expect(catalog.getByRole('region', { name: 'Files & documents' })).toBeVisible()
  for (const theme of ['light', 'dark']) {
    await page.evaluate((value) => {
      const surface = document.querySelector('.app-surface')!
      surface.classList.toggle('theme-dark', value === 'dark')
      surface.classList.toggle('theme-light', value === 'light')
    }, theme)
    await desktop.app.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows()[0].setSize(800, 700),
    )
    await catalog.getByRole('button', { name: 'Connector category' }).click()
    await page.getByRole('option', { name: 'Messaging', exact: true }).click()
    await expect(catalog.locator('.st-connector-card')).toHaveCount(2)
    for (const image of await catalog.locator('.st-connector-logo').all()) {
      await image.scrollIntoViewIfNeeded()
      await expect
        .poll(() => image.evaluate((element) => (element as HTMLImageElement).naturalWidth))
        .toBeGreaterThan(0)
    }
    await expect
      .poll(() => catalog.evaluate((element) => element.scrollWidth <= element.clientWidth))
      .toBe(true)
    const dimensions = await catalog.evaluate((element) => {
      const search = element.querySelector('input[type=search]')!
      const dropdown = element.querySelector('.st-connector-category-filter button')!
      return [search, dropdown].map((control) => {
        const style = getComputedStyle(control)
        return [
          control.getBoundingClientRect().height,
          style.fontSize,
          style.borderRadius,
          style.backgroundColor,
        ]
      })
    })
    expect(dimensions[0]).toEqual(dimensions[1])
    await settings.screenshot({ path: testInfo.outputPath(`connectors-${theme}.png`) })
    await catalog.getByRole('button', { name: 'Connector category' }).click()
    await expect(page.getByRole('listbox', { name: 'Connector category' })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath(`connector-menu-${theme}.png`) })
    await page.keyboard.press('Escape')
    await expect(settings).toBeVisible()
  }
  await catalog.getByRole('button', { name: 'Connector category' }).click()
  await page.getByRole('option', { name: 'All categories', exact: true }).click()
  for (const image of await catalog.locator('.st-connector-logo').all()) {
    await image.scrollIntoViewIfNeeded()
    await expect
      .poll(() => image.evaluate((element) => (element as HTMLImageElement).naturalWidth))
      .toBeGreaterThan(0)
  }
  expect(externalImages).toEqual([])
})
