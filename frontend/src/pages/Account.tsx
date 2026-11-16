import { useState, type FormEvent } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { Err } from '../ui'

export default function Account() {
  const { user } = useAuth()
  const [f, setF] = useState({ current_password: '', new_password: '', confirm: '' })
  const [error, setError] = useState('')
  const [success, setSuccess] = useState(false)
  const [busy, setBusy] = useState(false)

  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value })

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setSuccess(false)
    if (f.new_password.length < 10) return setError('New password must be at least 10 characters.')
    if (f.new_password !== f.confirm) return setError('Passwords do not match.')
    setBusy(true)
    try {
      await api('/auth/change-password', {
        method: 'POST',
        json: { current_password: f.current_password, new_password: f.new_password },
      })
      setSuccess(true)
      setF({ current_password: '', new_password: '', confirm: '' })
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="wrap" style={{ maxWidth: 440 }}>
      <h1>Account</h1>
      <div className="card">
        <p><strong>Name:</strong> {user?.name}</p>
        <p><strong>Email:</strong> {user?.email}</p>
        <p><strong>Role:</strong> {user?.role.toLowerCase()}</p>
      </div>

      <h2>Change password</h2>
      <form className="card" onSubmit={submit}>
        <label htmlFor="cur-pwd">Current password</label>
        <input id="cur-pwd" type="password" required value={f.current_password} onChange={set('current_password')} style={{ width: '100%' }} />
        <label htmlFor="new-pwd">New password (min 10 characters)</label>
        <input id="new-pwd" type="password" required minLength={10} value={f.new_password} onChange={set('new_password')} style={{ width: '100%' }} />
        <label htmlFor="confirm-pwd">Confirm new password</label>
        <input id="confirm-pwd" type="password" required minLength={10} value={f.confirm} onChange={set('confirm')} style={{ width: '100%' }} />
        <Err msg={error} />
        {success && <p style={{ color: 'var(--green, #22c55e)', marginTop: '.5rem' }}>Password changed successfully.</p>}
        <button className="primary" disabled={busy} style={{ marginTop: '.75rem' }}>
          {busy ? 'Saving…' : 'Change password'}
        </button>
      </form>
    </div>
  )
}
