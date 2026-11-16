import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, upload } from '../api'
import { Err } from '../ui'

export default function Upload() {
  const nav = useNavigate()
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const [pct, setPct] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [scanNow, setScanNow] = useState(true)

  const send = async (file?: File) => {
    if (!file) return
    setError('')
    setPct(0)
    try {
      const doc = await upload(file, setPct)
      if (scanNow) await api(`/documents/${doc.id}/scan`, { method: 'POST' })
      nav(`/documents/${doc.id}`)
    } catch (e) {
      setError((e as Error).message)
      setPct(null)
    }
  }
  return (
    <div className="wrap">
      <h1>Upload a document</h1>
      <div
        className={`drop ${over ? 'over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true) }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); void send(e.dataTransfer.files[0]) }}
      >
        <p>Drag a file here, or</p>
        <button className="primary" onClick={() => input.current?.click()} disabled={pct !== null}>Browse…</button>
        <input ref={input} type="file" hidden accept=".pdf,.txt,.docx,.png,.jpg,.jpeg" onChange={(e) => void send(e.target.files?.[0])} />
        <p className="muted">PDF, DOCX, TXT, PNG or JPEG. Maximum size is set by the server (20 MB by default).</p>
      </div>
      <label className="row"><input type="checkbox" checked={scanNow} onChange={(e) => setScanNow(e.target.checked)} /> Scan for personal data right after upload</label>
      {pct !== null && (
        <div role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <progress value={pct} max={100} style={{ width: '100%' }} /> <span className="muted">{pct === 100 ? 'Processing…' : `${pct}%`}</span>
        </div>
      )}
      <Err msg={error} />
    </div>
  )
}
