import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, download, type Doc, type Redacted } from '../api'
import { Badge, Err, fmtDate, fmtSize, useAction } from '../ui'

interface Reviewer { user_id: string; name: string; email: string }

export default function DocumentDetail() {
  const { id } = useParams()
  const nav = useNavigate()
  const qc = useQueryClient()
  const refresh = () => qc.invalidateQueries({ queryKey: ['doc', id] })
  const doc = useQuery({
    queryKey: ['doc', id], queryFn: () => api<Doc>(`/documents/${id}`),
    refetchInterval: (q) => (q.state.data?.scans?.some((s) => ['PENDING', 'PROCESSING'].includes(s.status)) ? 1500 : false),
  })
  const outputs = useQuery({ queryKey: ['redacted', id], queryFn: () => api<Redacted[]>(`/documents/${id}/redacted`) })
  const reviewers = useQuery({ queryKey: ['reviewers', id], queryFn: () => api<Reviewer[]>(`/documents/${id}/reviewers`) })
  const [email, setEmail] = useState('')

  const scan = useAction(() => api(`/documents/${id}/scan`, { method: 'POST' }), refresh)
  const retry = useAction((sid: string) => api(`/scans/${sid}/retry`, { method: 'POST' }), refresh)
  const del = useAction(async () => {
    if (confirm('Delete this document, its results and redacted files? This cannot be undone.')) {
      await api(`/documents/${id}`, { method: 'DELETE' })
      nav('/documents')
    }
  })
  const dl = useAction((path: string, name: string) => download(path, name))
  const assign = useAction(() => api(`/documents/${id}/reviewers`, { method: 'POST', json: { email } }), () => { setEmail(''); void qc.invalidateQueries({ queryKey: ['reviewers', id] }) })
  const unassign = useAction((uid: string) => api(`/documents/${id}/reviewers/${uid}`, { method: 'DELETE' }), () => qc.invalidateQueries({ queryKey: ['reviewers', id] }))
  const delOut = useAction(async (rid: string) => { if (confirm('Delete this redacted file?')) await api(`/redacted-documents/${rid}`, { method: 'DELETE' }) }, () => qc.invalidateQueries({ queryKey: ['redacted', id] }))

  const d = doc.data
  if (doc.error) return <div className="wrap"><Err msg={doc.error.message} /><Link to="/documents">Back</Link></div>
  if (!d) return <div className="wrap muted">Loading…</div>
  const active = d.scans?.some((s) => ['PENDING', 'PROCESSING'].includes(s.status))
  return (
    <div className="wrap">
      <p><Link to="/documents">← Documents</Link></p>
      <h1>{d.original_filename}</h1>
      <p className="muted">{d.file_type.toUpperCase()} · {fmtSize(d.file_size)} · uploaded {fmtDate(d.uploaded_at)} · <Badge v={d.status} /></p>
      <div className="row">
        <button className="primary" disabled={scan.busy || active} onClick={() => scan.run()}>{active ? 'Scan running…' : 'Scan for PII'}</button>
        <button disabled={dl.busy} onClick={() => dl.run(`/documents/${id}/download`, d.original_filename)}>Download original</button>
        <button className="danger" onClick={() => del.run()}>Delete</button>
      </div>
      <Err msg={scan.error || retry.error || del.error || dl.error} />

      <h2>Scans</h2>
      <div className="card">
        {!d.scans?.length && <p className="muted">Not scanned yet.</p>}
        <table>
          <thead><tr><th>Started</th><th>Status</th><th>Detection</th><th>Entities</th><th /></tr></thead>
          <tbody>
            {[...(d.scans ?? [])].reverse().map((s) => (
              <tr key={s.id}>
                <td>{fmtDate(s.started_at)}<div className="muted">{s.duration_ms != null && `${s.duration_ms} ms`}</div></td>
                <td><Badge v={s.status} />{s.error_message && <div className="error">{s.error_message}</div>}</td>
                <td>{s.detection_mode}<div className="muted">{s.model_version}</div>
                  {s.extraction_info?.warnings?.map((w) => <div key={w} className="warn">⚠ {w}</div>)}</td>
                <td>{s.entity_count}</td>
                <td>
                  {s.status === 'COMPLETED' && <Link className="btn primary" to={`/scans/${s.id}/review`}>Review</Link>}
                  {s.status === 'FAILED' && <button disabled={retry.busy} onClick={() => retry.run(s.id)}>Retry</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Redacted files</h2>
      <div className="card">
        {!outputs.data?.length && <p className="muted">None yet. Review a scan, then create a redacted file.</p>}
        <table>
          <tbody>
            {outputs.data?.map((r) => (
              <tr key={r.id}>
                <td>{fmtDate(r.created_at)}</td>
                <td>{r.redaction_count} redactions</td>
                <td>verification <Badge v={r.validation_status} /></td>
                <td className="row">
                  {r.validation_status !== 'FAILED' && <button onClick={() => dl.run(`/redacted-documents/${r.id}/download`, `redacted_${d.original_filename}`)}>Download</button>}
                  <button className="danger" onClick={() => delOut.run(r.id)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <Err msg={delOut.error} />
      </div>

      <h2>Reviewers</h2>
      <div className="card">
        <p className="muted">People you add here can review detections and create redacted files for this document. They cannot download or delete the original.</p>
        {reviewers.data?.map((r) => (
          <div className="row" key={r.user_id}>{r.name} ({r.email}) <button className="danger" onClick={() => unassign.run(r.user_id)}>Remove</button></div>
        ))}
        <form className="row" onSubmit={(e) => { e.preventDefault(); void assign.run() }}>
          <input type="email" required placeholder="reviewer@example.com" aria-label="Reviewer email" value={email} onChange={(e) => setEmail(e.target.value)} />
          <button disabled={assign.busy}>Add reviewer</button>
        </form>
        <Err msg={assign.error || unassign.error} />
      </div>
    </div>
  )
}
