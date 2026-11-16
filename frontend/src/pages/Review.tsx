import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, download, type Entity, type Page, type Redacted, type Scan } from '../api'
import { Badge, Err, useAction } from '../ui'

const TYPES = ['PERSON', 'AADHAAR', 'PAN', 'PASSPORT', 'VOTER_ID', 'DRIVING_LICENSE', 'PHONE', 'EMAIL', 'ADDRESS', 'DATE_OF_BIRTH',
  'BANK_ACCOUNT', 'IFSC', 'UPI_ID', 'VEHICLE_REG', 'CUSTOMER_ID', 'EMPLOYEE_ID', 'OTHER_IDENTIFIER']
const CLS: Record<string, string> = { REDACT: 'redact', KEEP: 'keep', FALSE_POSITIVE: 'fp', PENDING: '' }

interface TextOut { text: string; quality: { warnings?: string[] } | null }
interface EntPage extends Page<Entity> { counts_by_status: Record<string, number> }

/** Splits text into plain and highlighted pieces. Pieces are React text nodes, so document content is never parsed as HTML. */
export function segments(text: string, ents: Entity[]) {
  const out: { start: number; end: number; ent?: Entity }[] = []
  let pos = 0
  for (const e of [...ents].sort((a, b) => a.start_position - b.start_position || b.end_position - a.end_position)) {
    if (e.start_position < pos || e.end_position > text.length) continue // overlapping entities stay in the list only
    if (e.start_position > pos) out.push({ start: pos, end: e.start_position })
    out.push({ start: e.start_position, end: e.end_position, ent: e })
    pos = e.end_position
  }
  if (pos < text.length) out.push({ start: pos, end: text.length })
  return out
}

export default function Review() {
  const { id } = useParams()
  const qc = useQueryClient()
  const reload = () => qc.invalidateQueries({ queryKey: ['review', id] })
  const scan = useQuery({ queryKey: ['review', id, 'scan'], queryFn: () => api<Scan>(`/scans/${id}`) })
  const text = useQuery({ queryKey: ['review', id, 'text'], queryFn: () => api<TextOut>(`/scans/${id}/text`) })
  const ents = useQuery({
    queryKey: ['review', id, 'entities'],
    queryFn: async () => {
      let page = 1
      const all: Entity[] = []
      let last!: EntPage
      do {
        last = await api<EntPage>(`/scans/${id}/entities?page=${page}&page_size=200`)
        all.push(...last.items)
      } while (all.length < last.total && ++page)
      return { items: all, counts: last.counts_by_status }
    },
  })
  const outputs = useQuery({
    queryKey: ['review', id, 'outputs'], enabled: !!scan.data,
    queryFn: () => api<Redacted[]>(`/documents/${scan.data!.document_id}/redacted`),
  })
  const [sel, setSel] = useState<string | null>(null)
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [bulk, setBulk] = useState({ action: 'REDACT', min: '0.9', onlyPending: true })
  const [style, setStyle] = useState('full')
  const [pick, setPick] = useState<{ start: number; end: number; text: string } | null>(null)
  const [pickType, setPickType] = useState('OTHER_IDENTIFIER')
  const [result, setResult] = useState<Redacted | null>(null)

  const decide = useAction((eid: string, action: string) => api(`/entities/${eid}/review`, { method: 'POST', json: { action } }), reload)
  const retype = useAction((eid: string, t: string) => api(`/entities/${eid}`, { method: 'PATCH', json: { entity_type: t } }), reload)
  const bulkRun = useAction(() => api(`/scans/${id}/reviews/bulk`, {
    method: 'POST',
    json: {
      action: bulk.action,
      min_confidence: Number(bulk.min),
      only_pending: bulk.onlyPending,
    },
  }), reload)
  const addManual = useAction(async () => {
    if (pick) await api(`/scans/${id}/entities`, { method: 'POST', json: { start: pick.start, end: pick.end, entity_type: pickType } })
    setPick(null)
  }, reload)
  const delManual = useAction((eid: string) => api(`/entities/${eid}`, { method: 'DELETE' }), reload)
  const dl = useAction((rid: string) => download(`/redacted-documents/${rid}/download`, 'redacted-file'))
  const redact = useAction(async () => {
    const c = ents.data!.counts
    const msg = `Create a redacted file?\n\n${c.REDACT ?? 0} item(s) will be redacted.\n${c.PENDING ?? 0} item(s) are still unreviewed and will be LEFT IN the file.`
    if (confirm(msg)) setResult(await api<Redacted>(`/scans/${id}/redact`, { method: 'POST', json: { mask_style: style } }))
  }, () => qc.invalidateQueries({ queryKey: ['review', id, 'outputs'] }))

  const items = useMemo(() => ents.data?.items ?? [], [ents.data])
  const shown = useMemo(() => items.filter((e) => (!typeFilter || e.entity_type === typeFilter) && (!statusFilter || e.review_status === statusFilter)), [items, typeFilter, statusFilter])
  const segs = useMemo(() => (text.data ? segments(text.data.text, items) : []), [text.data, items])
  const counts = ents.data?.counts ?? {}

  // Turn a mouse selection inside the document view into character offsets of the extracted text.
  const onSelect = () => {
    const s = window.getSelection()
    if (!s || s.isCollapsed || !s.anchorNode || !s.focusNode) return setPick(null)
    const off = (n: Node, o: number) => {
      const el = (n.nodeType === 3 ? n.parentElement : (n as Element))?.closest<HTMLElement>('[data-start]')
      return el ? Number(el.dataset.start) + o : null
    }
    const a = off(s.anchorNode, s.anchorOffset), b = off(s.focusNode, s.focusOffset)
    if (a === null || b === null || !text.data) return setPick(null)
    let [start, end] = a < b ? [a, b] : [b, a]
    while (start < end && /\s/.test(text.data.text[start])) start++
    while (end > start && /\s/.test(text.data.text[end - 1])) end--
    setPick(end > start ? { start, end, text: text.data.text.slice(start, end) } : null)
  }

  if (scan.error || text.error) return <div className="wrap"><Err msg={(scan.error ?? text.error)!.message} /></div>
  return (
    <div className="wrap">
      <p><Link to={`/documents/${scan.data?.document_id ?? ''}`}>← Document</Link></p>
      <h1>Review detections</h1>
      <p className="muted">
        {scan.data && <>Detection: {scan.data.detection_mode} ({scan.data.model_version}). </>}
        Detections are model/rule <b>suggestions</b> until you decide. Pending {counts.PENDING ?? 0} · Redact {counts.REDACT ?? 0} · Keep {counts.KEEP ?? 0} · False positive {counts.FALSE_POSITIVE ?? 0}
      </p>
      {text.data?.quality?.warnings?.map((w) => <p key={w} className="warn">⚠ {w}</p>)}
      <Err msg={ents.error?.message || decide.error || retype.error || bulkRun.error || addManual.error || delManual.error} />

      <div className="cols">
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Document text</h2>
          <p className="muted">Click a highlight to select it. Select any text to add something the detector missed.</p>
          {pick && (
            <div className="row card">
              <span>Add “{pick.text.length > 40 ? pick.text.slice(0, 40) + '…' : pick.text}” as</span>
              <select aria-label="Type for manual entity" value={pickType} onChange={(e) => setPickType(e.target.value)}>{TYPES.map((t) => <option key={t}>{t}</option>)}</select>
              <button className="primary" onClick={() => addManual.run()}>Add and redact</button>
              <button onClick={() => setPick(null)}>Cancel</button>
            </div>
          )}
          <div className="doc" onMouseUp={onSelect} data-testid="doc">
            {text.isLoading && 'Loading…'}
            {segs.map((s) =>
              s.ent ? (
                <mark key={s.start} data-start={s.start} className={`${CLS[s.ent.review_status]} ${sel === s.ent.id ? 'sel' : ''}`}
                  title={`${s.ent.entity_type} · ${Math.round(s.ent.confidence_score * 100)}%`} onClick={() => setSel(s.ent!.id)}>
                  {text.data!.text.slice(s.start, s.end)}
                </mark>
              ) : (
                <span key={s.start} data-start={s.start}>{text.data!.text.slice(s.start, s.end)}</span>
              ),
            )}
          </div>
        </div>

        <div>
          <div className="card">
            <h2 style={{ marginTop: 0 }}>Bulk action</h2>
            <div className="row">
              <select aria-label="Bulk action" value={bulk.action} onChange={(e) => setBulk({ ...bulk, action: e.target.value })}>
                <option value="REDACT">Redact</option><option value="KEEP">Keep</option><option value="FALSE_POSITIVE">False positive</option>
              </select>
              <span>{bulk.onlyPending ? <>all <b>pending</b></> : <><b>all</b></>} with confidence ≥</span>
              <input aria-label="Minimum confidence" type="number" min="0" max="1" step="0.05" value={bulk.min} onChange={(e) => setBulk({ ...bulk, min: e.target.value })} style={{ width: 70 }} />
              <button disabled={bulkRun.busy} onClick={() => bulkRun.run()}>Apply</button>
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: '.4rem', marginTop: '.5rem', fontSize: '.85rem', cursor: 'pointer', color: 'var(--muted, #64748b)' }}>
              <input
                type="checkbox"
                checked={!bulk.onlyPending}
                onChange={(e) => setBulk({ ...bulk, onlyPending: !e.target.checked })}
              />
              Re-apply to already reviewed items
            </label>
          </div>

          <div className="card list">
            <div className="row" style={{ marginBottom: '.5rem' }}>
              <select aria-label="Filter by type" value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}><option value="">All types</option>{TYPES.map((t) => <option key={t}>{t}</option>)}</select>
              <select aria-label="Filter by status" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}><option value="">All</option>{['PENDING', 'REDACT', 'KEEP', 'FALSE_POSITIVE'].map((t) => <option key={t}>{t}</option>)}</select>
            </div>
            {ents.isLoading && <p className="muted">Loading…</p>}
            {ents.data && shown.length === 0 && <p className="muted">No detections{items.length ? ' match the filters' : ' in this document'}.</p>}
            {shown.map((e) => (
              <div key={e.id} className={`ent ${sel === e.id ? 'sel' : ''}`} onClick={() => setSel(e.id)}>
                <div className="row">
                  <select aria-label={`Type of ${e.masked_value}`} value={e.entity_type} onChange={(ev) => retype.run(e.id, ev.target.value)}>{TYPES.map((t) => <option key={t}>{t}</option>)}</select>
                  <code>{e.masked_value}</code>
                </div>
                <div className="muted">
                  {Math.round(e.confidence_score * 100)}% · {e.detection_method}{e.page_number ? ` · page ${e.page_number}` : ''}
                  {e.confidence_score < 0.6 && <span className="warn"> · low confidence</span>}
                  {e.entity_type !== e.original_entity_type && ` · was ${e.original_entity_type}`}
                </div>
                <div className="row">
                  <Badge v={e.review_status} /> <span className="muted">{e.review_status === 'PENDING' ? 'model suggestion' : 'human decision'}</span>
                </div>
                <div className="row">
                  {(['REDACT', 'KEEP', 'FALSE_POSITIVE'] as const).map((a) => (
                    <button key={a} disabled={decide.busy || e.review_status === a} onClick={(ev) => { ev.stopPropagation(); decide.run(e.id, a) }}>
                      {a === 'FALSE_POSITIVE' ? 'False positive' : a[0] + a.slice(1).toLowerCase()}
                    </button>
                  ))}
                  {e.is_manual && <button className="danger" onClick={(ev) => { ev.stopPropagation(); delManual.run(e.id) }}>Remove</button>}
                </div>
              </div>
            ))}
          </div>

          <div className="card">
            <h2 style={{ marginTop: 0 }}>Create redacted file</h2>
            <div className="row">
              <label htmlFor="style" style={{ margin: 0 }}>Text masking (TXT/DOCX)</label>
              <select id="style" value={style} onChange={(e) => setStyle(e.target.value)}>
                <option value="full">Full</option><option value="partial">Keep last 4 digits</option>
              </select>
              <button className="primary" disabled={redact.busy || !counts.REDACT} onClick={() => redact.run()}>
                {redact.busy ? 'Working…' : `Redact ${counts.REDACT ?? 0} item(s)`}
              </button>
            </div>
            <p className="muted">PDFs and images are always fully blacked out. Your original is never changed.</p>
            <Err msg={redact.error || dl.error} />
            {result && (
              <p>Verification: <Badge v={result.validation_status} />{' '}
                {result.validation_status === 'FAILED' && <span className="error">The redacted file did not pass verification and was not saved.</span>}
                {result.validation_status === 'SKIPPED' && <span className="warn">Could not be verified automatically. Check it manually.</span>}
                {result.validation_status !== 'FAILED' && <button className="primary" onClick={() => dl.run(result.id)}>Download redacted file</button>}
              </p>
            )}
            {outputs.data && outputs.data.length > 0 && <p className="muted">{outputs.data.length} redacted file(s) exist for this document (see the document page).</p>}
          </div>
        </div>
      </div>
    </div>
  )
}
