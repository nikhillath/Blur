import { useState } from 'react'

export const fmtDate = (s: string | null) => (s ? new Date(s).toLocaleString() : '-')
export const fmtSize = (n: number) => (n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`)

const COLORS: Record<string, string> = {
  COMPLETED: 'ok', SCANNED: 'ok', PASSED: 'ok', REDACT: 'bad', FAILED: 'bad', KEEP: 'ok',
  PENDING: 'warn', PROCESSING: 'warn', UPLOADED: '', SKIPPED: 'warn', FALSE_POSITIVE: '',
}
export const Badge = ({ v }: { v: string }) => <span className={`badge ${COLORS[v] ?? ''}`}>{v.replace('_', ' ')}</span>
export const Err = ({ msg }: { msg?: string }) => (msg ? <p className="error" role="alert">{msg}</p> : null)

/** Runs an async action, tracking busy state and showing the API error message. */
export function useAction<A extends unknown[]>(fn: (...a: A) => Promise<unknown>, after?: () => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const run = async (...a: A) => {
    setBusy(true)
    setError('')
    try {
      await fn(...a)
      after?.()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return { run, busy, error }
}

export function Pager({ page, total, size, set }: { page: number; total: number; size: number; set: (p: number) => void }) {
  const last = Math.max(1, Math.ceil(total / size))
  return (
    <div className="row" style={{ marginTop: '.5rem' }}>
      <button disabled={page <= 1} onClick={() => set(page - 1)}>Prev</button>
      <span className="muted">Page {page} of {last} ({total} total)</span>
      <button disabled={page >= last} onClick={() => set(page + 1)}>Next</button>
    </div>
  )
}
