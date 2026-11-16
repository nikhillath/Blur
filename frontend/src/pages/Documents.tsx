import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Doc, type Page } from '../api'
import { Badge, Err, fmtDate, fmtSize, Pager, useAction } from '../ui'

export default function Documents() {
  const qc = useQueryClient()
  const [page, setPage] = useState(1)
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('')
  const params = new URLSearchParams({ page: String(page), page_size: '15', ...(q && { q }), ...(status && { status }) })
  const docs = useQuery({
    queryKey: ['documents', page, q, status], queryFn: () => api<Page<Doc>>(`/documents?${params}`),
    refetchInterval: (query) => (query.state.data?.items.some((d) => d.status === 'PROCESSING') ? 2000 : false),
  })
  const del = useAction(
    async (id: string) => { if (confirm('Delete this document and all its results? This cannot be undone.')) await api(`/documents/${id}`, { method: 'DELETE' }) },
    () => qc.invalidateQueries({ queryKey: ['documents'] }),
  )
  return (
    <div className="wrap">
      <div className="row"><h1 style={{ flex: 1 }}>Documents</h1><Link className="btn primary" to="/upload">Upload</Link></div>
      <div className="row" style={{ marginBottom: '.75rem' }}>
        <input placeholder="Search filename" aria-label="Search filename" value={q} onChange={(e) => { setQ(e.target.value); setPage(1) }} />
        <select aria-label="Status filter" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
          <option value="">All statuses</option>
          {['UPLOADED', 'PROCESSING', 'SCANNED', 'FAILED'].map((s) => <option key={s}>{s}</option>)}
        </select>
      </div>
      <Err msg={docs.error?.message ?? del.error} />
      <div className="card">
        <table>
          <thead><tr><th>File</th><th>Uploaded</th><th>Status</th><th>Scans</th><th>Latest scan</th><th /></tr></thead>
          <tbody>
            {docs.data?.items.map((d) => (
              <tr key={d.id}>
                <td><Link to={`/documents/${d.id}`}>{d.original_filename}</Link><div className="muted">{d.file_type} · {fmtSize(d.file_size)}</div></td>
                <td>{fmtDate(d.uploaded_at)}</td>
                <td><Badge v={d.status} /></td>
                <td>{d.scan_count}</td>
                <td>{d.latest_scan ? <><Badge v={d.latest_scan.status} /> {d.latest_scan.entity_count} entities</> : '-'}</td>
                <td><button className="danger" disabled={del.busy} onClick={() => del.run(d.id)}>Delete</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        {docs.isLoading && <p className="muted">Loading…</p>}
        {docs.data?.items.length === 0 && <p className="muted">No documents found.</p>}
      </div>
      {docs.data && <Pager page={page} total={docs.data.total} size={15} set={setPage} />}
    </div>
  )
}
