// Runs electron/*.test.cjs with the Node test runner. The files are listed here
// because Node 20 does not expand glob patterns given to --test and npm does not
// expand them on Windows.
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const electronDir = path.join(__dirname, '..', 'electron')
const files = fs.readdirSync(electronDir)
  .filter((name) => name.endsWith('.test.cjs'))
  .sort()
  .map((name) => path.join(electronDir, name))

if (files.length === 0) {
  console.error(`No Electron tests found in ${electronDir}`)
  process.exit(1)
}

const result = spawnSync(process.execPath, ['--test', ...files], { stdio: 'inherit' })
process.exit(result.status ?? 1)
