import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { backendEnv } from '../playwright.config'

export default function setup() {
  const backend = path.resolve(import.meta.dirname, '../../backend')
  execFileSync(path.join(backend, '.venv/Scripts/python.exe'), ['-m', 'scripts.e2e_seed'], { cwd: backend, env: backendEnv, stdio: 'inherit' })
}
