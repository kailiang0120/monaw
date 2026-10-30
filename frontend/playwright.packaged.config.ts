import { defineConfig } from '@playwright/test'

// Checks the packaged app built by scripts/package-windows.ps1 (see docs/packaging.md).
export default defineConfig({
  testDir: './e2e-packaged',
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  outputDir: './test-results/packaged',
  reporter: [['list'], ['junit', { outputFile: './test-results/packaged.xml' }]],
})
