// Small fetch wrapper. The access token lives in memory only; the refresh token is an httpOnly cookie.
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export interface User { id: string; name: string; email: string; role: 'USER' | 'REVIEWER' | 'ADMIN'; is_active: boolean; created_at: string }
export interface Scan {
  id: string; document_id: string; model_version: string; detection_mode: string; status: string
  started_at: string; completed_at: string | null; duration_ms: number | null; error_message: string | null
  extraction_info: { warnings?: string[]; char_count?: number } | null; detector_config: Record<string, unknown> | null; entity_count: number
}
export interface Doc {
  id: string; original_filename: string; file_type: string; file_size: number; status: string; uploaded_at: string
  processed_at: string | null; scan_count?: number; latest_scan?: Scan | null; scans?: Scan[]
}
export interface Entity {
  id: string; entity_type: string; original_entity_type: string; masked_value: string; confidence_score: number
  start_position: number; end_position: number; page_number: number | null; detection_method: string
  review_status: 'PENDING' | 'REDACT' | 'KEEP' | 'FALSE_POSITIVE'; is_manual: boolean
}
export interface Redacted {
  id: string; original_document_id: string; file_type: string; redaction_count: number
  validation_status: 'PASSED' | 'FAILED' | 'SKIPPED'; validation_details: Record<string, unknown> | null; created_at: string
}
export interface Page<T> { items: T[]; total: number; page: number; page_size: number }

const RETRY_MS = 400
let token: string | null = null
let refreshing: Promise<boolean> | null = null
export const setToken = (t: string | null) => { token = t }

async function refresh(): Promise<boolean> {
  const post = () => fetch('/api/v1/auth/refresh', { method: 'POST', credentials: 'include' })
  refreshing ??= (async () => {
    let r = await post()
    if (r.status === 401) { // another tab may have just rotated the cookie: try once more with the new one
      await new Promise((ok) => setTimeout(ok, RETRY_MS))
      r = await post()
    }
    if (!r.ok) return false
    token = (await r.json()).access_token
    return true
  })().catch(() => false).finally(() => { refreshing = null })
  return refreshing
}

/** Turns FastAPI's detail (a string, or a list of validation errors) into one readable sentence. */
export function errorText(detail: unknown): string | undefined {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((e: { loc?: unknown[]; msg?: string }) => {
      const field = (e.loc ?? []).filter((p) => p !== 'body' && typeof p === 'string').join('.')
      return `${field ? field + ': ' : ''}${e.msg ?? 'invalid value'}`
    }).join('; ')
  }
  return undefined
}

async function request(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const res = await fetch(`/api/v1${path}`, { ...init, headers, credentials: 'include' })
  if (res.status === 401 && retry && !path.startsWith('/auth/') && (await refresh())) return request(path, init, false)
  if (!res.ok) {
    let msg = res.statusText
    try {
      msg = errorText((await res.json()).detail) ?? msg
    } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, msg)
  }
  return res
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = init
  const res = await request(path, json !== undefined ? { ...rest, body: JSON.stringify(json) } : rest)
  return res.status === 204 ? (undefined as T) : res.json()
}

export async function download(path: string, fallbackName: string) {
  const res = await request(path)
  const cd = res.headers.get('content-disposition') ?? ''
  const m = /filename\*=utf-8''([^;]+)/i.exec(cd) ?? /filename="?([^";]+)"?/i.exec(cd)
  const url = URL.createObjectURL(await res.blob())
  const a = Object.assign(document.createElement('a'), { href: url, download: m ? decodeURIComponent(m[1]) : fallbackName })
  a.click()
  URL.revokeObjectURL(url)
}

export function upload(file: File, onProgress: (pct: number) => void): Promise<Doc> {
  return new Promise((resolve, reject) => {
    const send = (retry: boolean) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/v1/documents')
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((100 * e.loaded) / e.total))
      xhr.onload = async () => {
        if (xhr.status === 401 && retry && (await refresh())) return send(false)
        let body: { detail?: unknown } = {}
        try { body = JSON.parse(xhr.responseText) } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body as unknown as Doc)
        else reject(new ApiError(xhr.status, typeof body.detail === 'string' ? body.detail : 'upload failed'))
      }
      xhr.onerror = () => reject(new ApiError(0, 'network error'))
      const fd = new FormData()
      fd.append('file', file)
      xhr.send(fd)
    }
    send(true)
  })
}

export const tryRestoreSession = async (): Promise<User | null> => {
  if (!(await refresh())) return null
  return api<User>('/auth/me')
}
