import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, type Doc, type Page } from '../api'
import { useAuth } from '../auth'
import { Err, fmtDate } from '../ui'

interface Stats {
  documents: number; by_status: Record<string, number>; failed_scans: number; redacted_documents: number
  pending_entities: number; awaiting_review_documents: number
  recent_activity: { action: string; created_at: string; document_id: string | null }[]
}

export default function Dashboard() {
  const { user } = useAuth()
  const stats = useQuery({ queryKey: ['dashboard'], queryFn: () => api<Stats>('/dashboard') })
  const assigned = useQuery({
    queryKey: ['assigned'], queryFn: () => api<Page<Doc>>('/review/assigned'), enabled: user?.role !== 'USER',
  })
  const s = stats.data
  const box = (label: string, v: number | undefined) => (
    <div className="card stat"><b>{v ?? '…'}</b><span className="muted">{label}</span></div>
  )
  return (
    <div className="wrap">
      <h1>Dashboard</h1>
      <Err msg={stats.error?.message} />
      <div className="grid">
        {box('Documents', s?.documents)}
        {box('Scanned', s?.by_status.SCANNED)}
        {box('Awaiting review', s?.awaiting_review_documents)}
        {box('Entities to review', s?.pending_entities)}
        {box('Failed scans', s?.failed_scans)}
        {box('Redacted files', s?.redacted_documents)}
      </div>
      {s && s.by_status.PROCESSING ? <p className="muted">{s.by_status.PROCESSING} document(s) processing…</p> : null}
      {assigned.data && assigned.data.items.length > 0 && (
        <div className="card">
          <h2>Assigned to you for review</h2>
          <table>
            <tbody>
              {assigned.data.items.map((d) => (
                <tr key={d.id}>
                  <td>{d.original_filename}</td>
                  <td>{d.latest_scan ? `${d.latest_scan.entity_count} entities` : 'not scanned'}</td>
                  <td>{d.latest_scan?.status === 'COMPLETED' && <Link to={`/scans/${d.latest_scan.id}/review`}>Review</Link>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="card">
        <h2>Recent activity</h2>
        {s?.recent_activity.length === 0 && <p className="muted">Nothing yet. <Link to="/upload">Upload a document</Link> to get started.</p>}
        <table>
          <tbody>
            {s?.recent_activity.map((a, i) => (
              <tr key={i}>
                <td>{a.action.replaceAll('_', ' ').toLowerCase()}</td>
                <td className="muted">{fmtDate(a.created_at)}</td>
                <td>{a.document_id && <Link to={`/documents/${a.document_id}`}>document</Link>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
