import { test as base, expect, _electron, type ElectronApplication, type Page } from '@playwright/test'
import { mkdtemp, mkdir, writeFile, readFile, rm, access } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve, join, sep } from 'node:path'
import { createServer, connect } from 'node:net'
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'

const require = createRequire(import.meta.url)
const frontend = resolve(import.meta.dirname, '..')
// Playwright forces colour in workers; without --color never uv wraps these paths in escape codes.
const uvCache = execFileSync('uv', ['--color', 'never', 'cache', 'dir'], { encoding: 'utf8', windowsHide: true }).trim()
const python = execFileSync('uv', ['--color', 'never', 'python', 'find', '--project', resolve(frontend, '../backend')], { encoding: 'utf8', windowsHide: true }).trim()

async function freePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((done, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', done) })
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('No application-test port')
  await new Promise<void>((done) => server.close(() => done()))
  return address.port
}

async function portIsOpen(port: number): Promise<boolean> {
  return new Promise((done) => {
    const socket = connect({ host: '127.0.0.1', port })
    const finish = (open: boolean) => { socket.destroy(); done(open) }
    socket.once('connect', () => finish(true)).once('error', () => finish(false))
    socket.setTimeout(500, () => finish(false))
  })
}

function assertDisposable(root: string) {
  if (!root.startsWith(resolve(tmpdir()) + sep) || !root.split(sep).at(-1)?.startsWith('monaw-e2e-')) {
    throw new Error('Refusing to clean an application-test path outside its disposable directory')
  }
}

export class Desktop {
  app!: ElectronApplication
  page!: Page
  errors: string[] = []
  consoleErrors: string[] = []
  mainOutput = ''
  constructor(readonly root: string, readonly port: number) {}

  async start() {
    const env = Object.fromEntries(Object.entries(process.env).filter(([key, value]) => value !== undefined
      && !/^(OPENAI|GOOGLE|GEMINI|TAVILY|TELEGRAM|CODEX|MONAW|AGENT|VITE|PYTHON|UV|ELECTRON_RUN_AS_NODE)/i.test(key))) as Record<string, string>
    Object.assign(env, {
      MONAW_E2E_ROOT: this.root, MONAW_HOME: this.root,
      AGENT_RUNTIME_DIR: join(this.root, 'runtime'), AGENT_WORKSPACE_DIR: join(this.root, 'workspace'),
      AGENT_MEMORY_DIR: join(this.root, 'memory'), MONAW_BACKEND_PORT: String(this.port),
      MONAW_RENDERER_MODE: 'bundle', MONAW_DISABLE_BUILTIN_MCP: '1',
      PYTHONPATH: join(frontend, 'e2e', 'fixtures', 'python'),
      UV_PROJECT_ENVIRONMENT: join(this.root, 'venv'), UV_PYTHON: python, UV_CACHE_DIR: uvCache,
      OPENAI_API_KEY: 'sk-monaw-e2e-fixture-only', GOOGLE_API_KEY: '', TAVILY_API_KEY: '', TELEGRAM_BOT_TOKEN: '',
      MODEL_PROVIDER: 'openai', MODEL_NAME: 'gpt-6.1-sol',
      REASONING_EFFORT: 'medium', PORT: String(this.port), ALLOW_ARBITRARY_APP_PATHS: 'false',
      TELEGRAM_ALLOWED_USER_IDS: '', TELEGRAM_ALLOWED_CHAT_IDS: '', TELEGRAM_ALLOW_ALL: 'false',
      APPDATA: join(this.root, 'appdata'), LOCALAPPDATA: join(this.root, 'localappdata'),
    })
    this.app = await _electron.launch({
      executablePath: require('electron'), args: [join(frontend, 'e2e', 'fixtures', 'electron-entry.cjs')],
      cwd: frontend, env, timeout: 45_000, chromiumSandbox: true,
    })
    const capture = (chunk: Buffer) => { this.mainOutput = (this.mainOutput + chunk.toString()).slice(-20_000) }
    this.app.process().stdout?.on('data', capture)
    this.app.process().stderr?.on('data', capture)
    this.page = await this.app.firstWindow()
    this.page.on('pageerror', (error) => this.errors.push(error.message))
    this.page.on('console', (message) => { if (message.type() === 'error' && this.consoleErrors.length < 30) this.consoleErrors.push(message.text()) })
    await this.page.route('**/*', (route) => {
      const url = new URL(route.request().url())
      return ['file:', 'data:', 'blob:'].includes(url.protocol)
        || (url.hostname === '127.0.0.1' && url.port === String(this.port)) ? route.continue() : route.abort()
    })
    await expect.poll(() => access(join(this.root, 'control', 'provider-installed')).then(() => true, () => false), { timeout: 45_000 }).toBe(true)
    await expect.poll(async () => this.page.request.get(`http://127.0.0.1:${this.port}/health`).then((r) => r.status(), () => 0), { timeout: 45_000 }).toBe(200)
    expect(await this.page.evaluate(() => window.electronAPI?.backendBaseUrl)).toBe(`http://127.0.0.1:${this.port}`)
    await expect.poll(async () => (await this.api('/api/settings')).status).toBe(200)
    await expect(this.page.getByRole('textbox', { name: 'Message', exact: true })).toBeEnabled()
  }

  async api(path: string, body?: unknown, method = body === undefined ? 'GET' : 'POST') {
    if (!path.startsWith('/api/')) throw new Error('Expected application API path')
    return this.page.evaluate(async ({ path, body, method }) => {
      const session = await window.electronAPI!.getControlSession()
      const response = await fetch(window.electronAPI!.backendBaseUrl + path, {
        method, headers: { Authorization: `Bearer ${session.token}`, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      return { status: response.status, data: await response.json() }
    }, { path, body, method })
  }

  async send(scenario: string) {
    await this.page.getByRole('textbox', { name: 'Message', exact: true }).fill(`[e2e:${scenario}]`)
    await this.page.getByRole('button', { name: 'Send message', exact: true }).click()
  }

  async signal(name: string) { await writeFile(join(this.root, 'control', name), '') }
  async started(name: string) {
    await expect.poll(() => access(join(this.root, 'control', `${name}.started`)).then(() => true, () => false)).toBe(true)
  }
  async events() {
    return readFile(join(this.root, 'control', 'events.jsonl'), 'utf8').then((value) => value.trim().split('\n').filter(Boolean).map((line) => JSON.parse(line)))
  }
  async close() {
    if (this.app && this.app.process().exitCode === null) {
      const closed = this.app.waitForEvent('close', { timeout: 15_000 })
      try {
        // Return from the inspector call before quitting the inspected process.
        // This exercises the real quit handlers without awaiting app.quit inside
        // the inspector's own synchronous evaluation frame.
        await this.app.evaluate(({ app }) => { setImmediate(() => app.quit()) })
        await closed
      } catch (error) {
        if (this.app.process().exitCode === null && process.platform === 'win32') {
          execFileSync('taskkill', ['/pid', String(this.app.process().pid), '/t', '/f'], { windowsHide: true, timeout: 5000, stdio: 'ignore' })
        }
        throw error
      }
    }
    await expect.poll(() => portIsOpen(this.port), { timeout: 15_000 }).toBe(false)
  }
}

export const test = base.extend<{ desktop: Desktop }>({
  desktop: async ({}, use, testInfo) => {
    const root = await mkdtemp(join(tmpdir(), 'monaw-e2e-'))
    assertDisposable(root)
    await Promise.all(['runtime', 'workspace', 'control', 'memory', 'appdata', 'localappdata'].map((dir) => mkdir(join(root, dir))))
    await writeFile(join(root, 'workspace', 'read.txt'), 'Completed sibling evidence\n')
    await writeFile(join(root, 'runtime', 'settings.json'), JSON.stringify({
      memory: { enabled: false, auto_learn: false }, mcp: { enabled: false, servers: [] },
      browser: { screenshots_dir: join(root, 'screenshots') },
      permissions: { mode: 'default', path_rules: [{ path: join(root, 'workspace'), read: true, write: true }] },
      sandbox: { mode: 'host', allowed_bind_roots: [join(root, 'workspace')] },
    }))
    const desktop = new Desktop(root, await freePort())
    let setupFailed = false
    try {
      await desktop.start()
      await use(desktop)
    } catch (error) {
      setupFailed = true
      throw error
    } finally {
      if (setupFailed || testInfo.status !== testInfo.expectedStatus) {
        await desktop.page?.screenshot({ path: testInfo.outputPath('application.png') }).catch(() => {})
        await writeFile(testInfo.outputPath('renderer-errors.json'), JSON.stringify({ uncaught: desktop.errors, console: desktop.consoleErrors }))
        await writeFile(testInfo.outputPath('electron.log'), desktop.mainOutput)
        await desktop.api('/api/conversations').then(async ({ data }) => {
          const evidence = []
          for (const conversation of data) {
            evidence.push((await desktop.api(`/api/conversations/${conversation.id}/messages`)).data)
          }
          await writeFile(testInfo.outputPath('persisted-messages.json'), JSON.stringify(evidence))
        }).catch(() => {})
        await testInfo.attach('renderer-errors', { body: JSON.stringify({ uncaught: desktop.errors, console: desktop.consoleErrors }), contentType: 'application/json' })
        for (const file of ['runtime/backend.log', 'control/events.jsonl']) {
          await readFile(join(root, file)).then((body) => testInfo.attach(file.replace('/', '-'), { body: body.subarray(-40_000), contentType: 'text/plain' }), () => {})
        }
      }
      try { await desktop.close() } finally {
        assertDisposable(root)
        await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 })
      }
    }
  },
})
export { expect }
