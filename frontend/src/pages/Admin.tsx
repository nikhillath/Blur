import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Page, type User } from '../api'
import { useAuth } from '../auth'
import { Err, fmtDate, Pager, useAction } from '../ui'

interface Log { id: string; user_id: string | null; document_id: string | null; action: string; details: Record<string, unknown> | null; created_at: string; seq: number | null }
interface VerifyResult { status: string; rows_checked: number; failures?: { seq: number; id: string; action: string }[] }

export default function Admin() {
  const { user: me } = useAuth()
  const qc = useQueryClient()
  const [up, setUp] = useState(1)
  const [lp, setLp] = useState(1)
  const [action, setAction] = useState('')
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null)
  const [verifying, setVerifying] = useState(false)

  // Create user form state
  const [showCreate, setShowCreate] = useState(false)
  const [newUser, setNewUser] = useState({ name: '', email: '', password: '', role: 'USER' })
  const [createError, setCreateError] = useState('')
  const [creating, setCreating] = useState(false)

  // Per-user password reset state
  const [resetTarget, setResetTarget] = useState<string | null>(null)
  const [resetPwd, setResetPwd] = useState('')
  const [resetError, setResetError] = useState('')

  const users = useQuery({ queryKey: ['admin-users', up], queryFn: () => api<Page<User>>(`/users?page=${up}&page_size=15`) })
  const logs = useQuery({ queryKey: ['admin-logs', lp, action], queryFn: () => api<Page<Log>>(`/audit-logs?page=${lp}&page_size=25${action ? `&action=${action}` : ''}`) })
  const patch = useAction((id: string, body: object) => api(`/users/${id}`, { method: 'PATCH', json: body }), () => qc.invalidateQueries({ queryKey: ['admin-users'] }))

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    setCreateError('')
    setCreating(true)
    try {
      await api('/users', { method: 'POST', json: newUser })
      setNewUser({ name: '', email: '', password: '', role: 'USER' })
      setShowCreate(false)
      qc.invalidateQueries({ queryKey: ['admin-users'] })
    } catch (err) { setCreateError((err as Error).message) }
    finally { setCreating(false) }
  }

  const handleResetPassword = async (userId: string) => {
    setResetError('')
    try {
      await api(`/users/${userId}`, { method: 'PATCH', json: { new_password: resetPwd } })
      setResetTarget(null)
      setResetPwd('')
    } catch (err) { setResetError((err as Error).message) }
  }

  const handleVerify = async () => {
    setVerifying(true)
    try {
      const r = await api<VerifyResult>('/audit-logs/verify')
      setVerifyResult(r)
    } catch (err) { setVerifyResult({ status: 'error', rows_checked: 0 }) }
    finally { setVerifying(false) }
  }

  return (
    <div className="wrap">
      <h1>Administration</h1>

      {/* ── Users ──────────────────────────────────────────────────────────── */}
      <h2>Users</h2>
      <Err msg={users.error?.message || patch.error} />
      <div style={{ marginBottom: '.75rem', display: 'flex', gap: '.5rem' }}>
        <button onClick={() => setShowCreate(!showCreate)}>
          {showCreate ? 'Cancel' : '+ Create user'}
        </button>
      </div>

      {showCreate && (
        <form className="card" onSubmit={handleCreate} style={{ maxWidth: 420, marginBottom: '1rem' }}>
          <h3 style={{ margin: '0 0 .5rem' }}>Create account</h3>
          <label htmlFor="new-name">Name</label>
          <input id="new-name" required value={newUser.name} onChange={e => setNewUser({ ...newUser, name: e.target.value })} style={{ width: '100%' }} />
          <label htmlFor="new-email">Email</label>
          <input id="new-email" type="email" required value={newUser.email} onChange={e => setNewUser({ ...newUser, email: e.target.value })} style={{ width: '100%' }} />
          <label htmlFor="new-password">Password (min 10 chars)</label>
          <input id="new-password" type="password" required minLength={10} value={newUser.password} onChange={e => setNewUser({ ...newUser, password: e.target.value })} style={{ width: '100%' }} />
          <label htmlFor="new-role">Role</label>
          <select id="new-role" value={newUser.role} onChange={e => setNewUser({ ...newUser, role: e.target.value })}>
            <option>USER</option><option>REVIEWER</option><option>ADMIN</option>
          </select>
          <Err msg={createError} />
          <button className="primary" disabled={creating} style={{ marginTop: '.5rem' }}>
            {creating ? 'Creating…' : 'Create'}
          </button>
        </form>
      )}

      <div className="card">
        <table>
          <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Active</th><th>Locked</th><th>Joined</th><th>Actions</th></tr></thead>
          <tbody>
            {users.data?.items.map((u) => (
              <tr key={u.id}>
                <td>{u.name}</td><td>{u.email}</td>
                <td>
                  <select aria-label={`Role of ${u.email}`} value={u.role} disabled={u.id === me?.id} onChange={(e) => patch.run(u.id, { role: e.target.value })}>
                    <option>USER</option><option>REVIEWER</option><option>ADMIN</option>
                  </select>
                </td>
                <td><input type="checkbox" aria-label={`Active ${u.email}`} checked={u.is_active} disabled={u.id === me?.id} onChange={(e) => patch.run(u.id, { is_active: e.target.checked })} /></td>
                <td className="muted">{(u as any).locked_until ? '🔒' : '—'}</td>
                <td>{fmtDate(u.created_at)}</td>
                <td style={{ display: 'flex', gap: '.4rem', flexWrap: 'wrap' }}>
                  {(u as any).locked_until && (
                    <button style={{ fontSize: '.8rem' }} onClick={() => patch.run(u.id, { unlock: true })}>Unlock</button>
                  )}
                  <button style={{ fontSize: '.8rem' }} onClick={() => { setResetTarget(u.id); setResetPwd(''); setResetError('') }}>Reset pwd</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {users.data && <Pager page={up} total={users.data.total} size={15} set={setUp} />}
      </div>

      {/* ── Password reset modal ──────────────────────────────────────────── */}
      {resetTarget && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
          <div className="card" style={{ minWidth: 320 }}>
            <h3>Reset password</h3>
            <label htmlFor="reset-pwd">New password (min 10 chars)</label>
            <input id="reset-pwd" type="password" minLength={10} value={resetPwd} onChange={e => setResetPwd(e.target.value)} style={{ width: '100%' }} />
            <Err msg={resetError} />
            <div style={{ display: 'flex', gap: '.5rem', marginTop: '.5rem' }}>
              <button className="primary" onClick={() => handleResetPassword(resetTarget)}>Save</button>
              <button onClick={() => setResetTarget(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Audit log ────────────────────────────────────────────────────── */}
      <h2>Audit log</h2>
      <div style={{ display: 'flex', gap: '.5rem', alignItems: 'center', marginBottom: '.5rem' }}>
        <input placeholder="Filter by action, e.g. LOGIN_FAILED" aria-label="Filter audit by action" value={action} onChange={(e) => { setAction(e.target.value.toUpperCase()); setLp(1) }} style={{ width: 280 }} />
        <button onClick={handleVerify} disabled={verifying}>{verifying ? 'Verifying…' : '🔗 Verify chain'}</button>
      </div>
      {verifyResult && (
        <div className="card" style={{ marginBottom: '.75rem', borderLeft: `4px solid ${verifyResult.status === 'ok' ? '#22c55e' : '#ef4444'}` }}>
          <strong>Audit chain: {verifyResult.status === 'ok' ? '✅ INTACT' : '❌ TAMPERED'}</strong>
          <span className="muted" style={{ marginLeft: '.5rem' }}>{verifyResult.rows_checked} rows checked</span>
          {verifyResult.failures?.map(f => (
            <div key={f.id} className="muted">seq={f.seq} id={f.id.slice(0, 8)} action={f.action}</div>
          ))}
        </div>
      )}
      <Err msg={logs.error?.message} />
      <div className="card">
        <table>
          <thead><tr><th>#</th><th>When</th><th>Action</th><th>User</th><th>Details</th></tr></thead>
          <tbody>
            {logs.data?.items.map((l) => (
              <tr key={l.id}>
                <td className="muted">{l.seq ?? '–'}</td>
                <td>{fmtDate(l.created_at)}</td><td>{l.action}</td>
                <td className="muted">{l.user_id?.slice(0, 8) ?? '-'}</td>
                <td className="muted">{l.details ? JSON.stringify(l.details) : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {logs.data && <Pager page={lp} total={logs.data.total} size={25} set={setLp} />}
      </div>
    </div>
  )
}
