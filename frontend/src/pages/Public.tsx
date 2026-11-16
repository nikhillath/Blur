import { useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { useAuth } from '../auth'
import { Err } from '../ui'

export function Landing() {
  const { data: authConfig } = useQuery({
    queryKey: ['auth-config'],
    queryFn: () => api<{ registration_open: boolean }>('/auth/config'),
    staleTime: 60_000,
  })
  const registrationOpen = authConfig?.registration_open ?? true

  return (
    <div className="wrap">
      <h1>PII Sentinel</h1>
      <p>Find personal information in documents, review what was found, and produce a redacted copy you can share.</p>
      <div className="card">
        <h2>How it works</h2>
        <ol>
          <li>Upload a PDF, DOCX, TXT or image (scanned pages are read with OCR).</li>
          <li>Rules and a trained language model suggest what looks like personal data. These are suggestions only.</li>
          <li>You review each suggestion: redact it, keep it, or mark it as a false positive. You can also add missed text.</li>
          <li>The app writes a new file with the selected text removed, checks the result, and lets you download it. Your original is never changed.</li>
        </ol>
        <p className="muted">Detection is automatic and imperfect. Always review before sharing a redacted file.</p>
      </div>
      <div className="row">
        <Link className="btn primary" to="/login">Log in</Link>
        {registrationOpen && <Link className="btn" to="/register">Create account</Link>}
      </div>
    </div>
  )
}

export function AuthPage({ mode }: { mode: 'login' | 'register' }) {
  const { user, login, register } = useAuth()
  const nav = useNavigate()
  const [f, setF] = useState({ name: '', email: '', password: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  if (user) return <Navigate to="/dashboard" replace />

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    if (mode === 'register' && f.password.length < 10) return setError('Password must be at least 10 characters.')
    setBusy(true)
    try {
      if (mode === 'login') await login(f.email, f.password)
      else await register(f.name, f.email, f.password)
      nav('/dashboard')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value })
  return (
    <div className="wrap" style={{ maxWidth: 380 }}>
      <h1>{mode === 'login' ? 'Log in' : 'Create account'}</h1>
      <form className="card" onSubmit={submit}>
        {mode === 'register' && (
          <>
            <label htmlFor="name">Name</label>
            <input id="name" required value={f.name} onChange={set('name')} style={{ width: '100%' }} />
          </>
        )}
        <label htmlFor="email">Email</label>
        <input id="email" type="email" required value={f.email} onChange={set('email')} style={{ width: '100%' }} />
        <label htmlFor="password">Password</label>
        <input id="password" type="password" required value={f.password} onChange={set('password')} style={{ width: '100%' }} />
        <Err msg={error} />
        <button className="primary" disabled={busy} style={{ marginTop: '.75rem' }}>
          {busy ? 'Please wait…' : mode === 'login' ? 'Log in' : 'Create account'}
        </button>
      </form>
      <p className="muted">
        {mode === 'login' ? <>No account? <Link to="/register">Register</Link></> : <>Have an account? <Link to="/login">Log in</Link></>}
      </p>
    </div>
  )
}
