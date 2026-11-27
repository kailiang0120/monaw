import { createHash } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const pack = require('@iconify-json/logos/icons.json')
const { version } = require('@iconify-json/logos/package.json')
const directory = new URL('../src/assets/connectors/', import.meta.url)
const selected = {
  github: 'github-icon',
  gmail: 'google-gmail',
  googlecalendar: 'google-calendar',
  googledrive: 'google-drive',
  one_drive: 'microsoft-onedrive',
  notion: 'notion-icon',
  slack: 'slack-icon',
  microsoft_teams: 'microsoft-teams',
  todoist: 'todoist-icon',
  linear: 'linear-icon',
}

/** Export only selected icons; builds and the installed app never contact an icon API. */
async function buildIcons() {
  const sources = JSON.parse(await readFile(new URL('sources.json', directory), 'utf8'))
  for (const [provider, name] of Object.entries(selected)) {
    const icon = pack.icons[name]
    if (!icon || icon.rotate || icon.hFlip || icon.vFlip)
      throw new Error(`Unsupported icon: ${name}`)
    const width = icon.width ?? pack.width ?? 16
    const height = icon.height ?? pack.height ?? 16
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}">${icon.body}</svg>\n`
    await writeFile(new URL(`${provider}.svg`, directory), svg)
    sources.icons[provider] = {
      source: `@iconify-json/logos@${version}:${name}`,
      license: 'CC0-1.0',
      sha256: createHash('sha256').update(svg).digest('hex'),
    }
  }
  await writeFile(new URL('sources.json', directory), `${JSON.stringify(sources, null, 2)}\n`)
  console.log(
    `Bundled ${Object.keys(selected).length} SVG Logos icons and the existing Outlook fallback.`,
  )
}

await buildIcons()
