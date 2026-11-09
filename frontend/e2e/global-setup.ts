import { mkdtemp, rm } from 'node:fs/promises'
import { realpathSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve, sep } from 'node:path'
import { execFileSync } from 'node:child_process'

// Build the backend environment once per run. A fresh venv per test copies every
// package from uv's cache (hardlinks fail across drives on CI), which can outlast
// the app's 30s backend startup window on a slow runner.
export default async function globalSetup() {
  const backend = resolve(import.meta.dirname, '..', '..', 'backend')
  const temporaryRoot = realpathSync.native(tmpdir())
  const venv = await mkdtemp(join(temporaryRoot, 'monaw-e2e-venv-'))
  const python = execFileSync('uv', ['--color', 'never', 'python', 'find', '--project', backend], { encoding: 'utf8', windowsHide: true }).trim()
  execFileSync('uv', ['--color', 'never', 'sync', '--locked', '--no-dev', '--project', backend], {
    env: { ...process.env, UV_PROJECT_ENVIRONMENT: venv, UV_PYTHON: python, UV_LINK_MODE: 'copy' },
    stdio: 'inherit', windowsHide: true, timeout: 600_000,
  })
  process.env.MONAW_E2E_VENV = venv
  return async () => {
    if (!venv.startsWith(temporaryRoot + sep) || !venv.split(sep).at(-1)?.startsWith('monaw-e2e-venv-')) return
    await rm(venv, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 })
  }
}
