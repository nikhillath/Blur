import { readFileSync } from 'node:fs'
import path from 'node:path'
import { defineConfig } from '@playwright/test'

// The E2E run uses the *_test database and its own storage folder, never the development data.
const env = Object.fromEntries(
  readFileSync(path.resolve(import.meta.dirname, '../.env'), 'utf8').split(/\r?\n/).filter((l) => l.includes('=') && !l.startsWith('#'))
    .map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]),
)
export const backendEnv = {
  ...process.env as Record<string, string>,
  DATABASE_URL: env.TEST_DATABASE_URL, STORAGE_DIR: path.resolve(import.meta.dirname, '../storage-e2e'),
  RATE_LIMIT_ENABLED: 'false', // limits are covered by backend unit tests; browser tests register many users from one address
  PYTHONIOENCODING: 'utf-8',
}

export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  expect: { timeout: 20_000 },
  workers: 1,
  reporter: [['list']],
  globalSetup: './e2e/global-setup.ts',
  use: { baseURL: 'http://localhost:5173', channel: 'msedge', acceptDownloads: true, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: [
    {
      command: `"${path.resolve(import.meta.dirname, '../backend/.venv/Scripts/python.exe')}" -m uvicorn app.main:app --port 8000`,
      cwd: '../backend', url: 'http://localhost:8000/health/ready', timeout: 120_000, reuseExistingServer: false, env: backendEnv,
    },
    { command: 'npm run dev', url: 'http://localhost:5173', reuseExistingServer: false },
  ],
})
