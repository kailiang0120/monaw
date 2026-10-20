import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  outputDir: './test-results/application',
  reporter: [['list'], ['junit', { outputFile: './test-results/application.xml' }]],
})
