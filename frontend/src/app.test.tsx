import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, errorText, setToken } from './api'
import { AuthProvider, RequireAuth } from './auth'
import Dashboard from './pages/Dashboard'
import Documents from './pages/Documents'
import { AuthPage } from './pages/Public'
import Review, { segments } from './pages/Review'

type Handler = (init?: RequestInit) => { status?: number; body?: unknown }
const calls: { method: string; path: string; body?: string }[] = []

/** Routes "METHOD /api/v1/path" (prefix match) to handlers; anything unmatched fails the test loudly. */
function mockApi(routes: Record<string, Handler>) {
  calls.length = 0
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = (init.method ?? 'GET').toUpperCase()
    const path = String(url).replace('/api/v1', '')
    calls.push({ method, path, body: init.body as string | undefined })
    const key = Object.keys(routes).find((k) => k === `${method} ${path}` || `${method} ${path}`.startsWith(k))
    if (!key) throw new Error(`unmocked request ${method} ${path}`)
    const { status = 200, body } = routes[key](init)
    return new Response(status === 204 ? null : JSON.stringify(body ?? {}), { status, headers: { 'content-type': 'application/json' } })
  }))
}

const USER = { id: 'u1', name: 'Alice', email: 'a@example.com', role: 'USER', is_active: true, created_at: '2026-01-01T00:00:00Z' }
const signedIn = { 'POST /auth/refresh': () => ({ body: { access_token: 't' } }), 'GET /auth/me': () => ({ body: USER }) }

function mount(ui: React.ReactNode, path = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}><AuthProvider>{ui}</AuthProvider></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => setToken(null))
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('api client', () => {
  it('refreshes once on 401 and retries the request', async () => {
    let n = 0
    mockApi({
      'GET /thing': () => (n++ === 0 ? { status: 401, body: { detail: 'expired' } } : { body: { ok: true } }),
      'POST /auth/refresh': () => ({ body: { access_token: 'new' } }),
    })
    expect(await api('/thing')).toEqual({ ok: true })
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual(['GET /thing', 'POST /auth/refresh', 'GET /thing'])
  })

  it('gives up with ApiError when refresh fails, and surfaces the server message', async () => {
    mockApi({ 'GET /thing': () => ({ status: 401, body: { detail: 'not authenticated' } }), 'POST /auth/refresh': () => ({ status: 401 }) })
    await expect(api('/thing')).rejects.toMatchObject({ status: 401, message: 'not authenticated' })
    mockApi({ 'GET /bad': () => ({ status: 422, body: { detail: 'span is outside the extracted text' } }) })
    const err = (await api('/bad').catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.message).toBe('span is outside the extracted text')
  })

  it('retries a refresh once when another tab rotated the cookie at the same moment', async () => {
    let refreshes = 0, hits = 0
    mockApi({
      'GET /thing': () => (hits++ === 0 ? { status: 401, body: { detail: 'expired' } } : { body: { ok: 1 } }),
      'POST /auth/refresh': () => (refreshes++ === 0 ? { status: 401, body: { detail: 'refresh in progress, retry' } } : { body: { access_token: 'n' } }),
    })
    expect(await api('/thing')).toEqual({ ok: 1 })
    expect(refreshes).toBe(2)
  })

  it('turns validation error lists into readable text, not raw JSON', async () => {
    const detail = [{ loc: ['body', 'email'], msg: 'value is not a valid email address', type: 'value_error' }, { loc: ['body', 'password'], msg: 'String should have at least 10 characters' }]
    expect(errorText(detail)).toBe('email: value is not a valid email address; password: String should have at least 10 characters')
    expect(errorText('plain message')).toBe('plain message')
    expect(errorText({ odd: 1 })).toBeUndefined()
    mockApi({ 'POST /auth/register': () => ({ status: 422, body: { detail } }) })
    await expect(api('/auth/register', { method: 'POST', json: {} })).rejects.toThrow(/^email: value is not a valid email address; password:/)
  })

  it('does not try to refresh for auth endpoints and handles 204', async () => {
    mockApi({ 'POST /auth/login': () => ({ status: 401, body: { detail: 'invalid credentials' } }), 'DELETE /x': () => ({ status: 204 }) })
    await expect(api('/auth/login', { method: 'POST', json: {} })).rejects.toThrow('invalid credentials')
    expect(calls).toHaveLength(1)
    expect(await api('/x', { method: 'DELETE' })).toBeUndefined()
  })
})

describe('auth pages and guards', () => {
  it('shows the server error on a failed login', async () => {
    mockApi({ 'POST /auth/refresh': () => ({ status: 401 }), 'POST /auth/login': () => ({ status: 401, body: { detail: 'invalid credentials' } }) })
    mount(<AuthPage mode="login" />)
    fireEvent.change(await screen.findByLabelText('Email'), { target: { value: 'a@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrong-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Log in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('invalid credentials')
  })

  it('rejects a short password on register without calling the server', async () => {
    mockApi({ 'POST /auth/refresh': () => ({ status: 401 }) })
    mount(<AuthPage mode="register" />)
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Alice' } })
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'short' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('at least 10 characters')
    expect(calls.some((c) => c.path === '/auth/register')).toBe(false)
  })

  it('redirects signed-out visitors to the login page', async () => {
    mockApi({ 'POST /auth/refresh': () => ({ status: 401 }) })
    mount(<Routes><Route path="/login" element={<p>login page</p>} /><Route path="/secret" element={<RequireAuth><p>secret</p></RequireAuth>} /></Routes>, '/secret')
    expect(await screen.findByText('login page')).toBeInTheDocument()
    expect(screen.queryByText('secret')).toBeNull()
  })

  it('sends a non-admin away from admin-only routes', async () => {
    mockApi(signedIn)
    mount(<Routes><Route path="/dashboard" element={<p>dash</p>} /><Route path="/admin" element={<RequireAuth roles={['ADMIN']}><p>admin area</p></RequireAuth>} /></Routes>, '/admin')
    expect(await screen.findByText('dash')).toBeInTheDocument()
    expect(screen.queryByText('admin area')).toBeNull()
  })
})

describe('dashboard and documents', () => {
  it('shows numbers from the API, not hard-coded values', async () => {
    mockApi({
      ...signedIn,
      'GET /dashboard': () => ({ body: { documents: 7, by_status: { SCANNED: 5 }, failed_scans: 2, redacted_documents: 3, pending_entities: 41, awaiting_review_documents: 4, recent_activity: [{ action: 'SCAN_COMPLETED', created_at: '2026-01-02T00:00:00Z', document_id: 'd1' }] } }),
    })
    mount(<RequireAuth><Dashboard /></RequireAuth>)
    expect(await screen.findByText('41')).toBeInTheDocument()
    for (const [value, label] of [['7', 'Documents'], ['2', 'Failed scans'], ['3', 'Redacted files'], ['4', 'Awaiting review']] as const) {
      expect(screen.getByText(label).parentElement).toHaveTextContent(value)
    }
    expect(screen.getByText('scan completed')).toBeInTheDocument()
  })

  it('shows an empty state and an error state for the document list', async () => {
    mockApi({ ...signedIn, 'GET /documents': () => ({ body: { items: [], total: 0, page: 1, page_size: 15 } }) })
    mount(<RequireAuth><Documents /></RequireAuth>)
    expect(await screen.findByText('No documents found.')).toBeInTheDocument()
    cleanup()
    mockApi({ ...signedIn, 'GET /documents': () => ({ status: 500, body: { detail: 'internal server error' } }) })
    mount(<RequireAuth><Documents /></RequireAuth>)
    expect(await screen.findByRole('alert')).toHaveTextContent('internal server error')
  })
})

describe('review page', () => {
  const TEXT = 'Contact <img src=x onerror=alert(1)> riya@example.com today'
  const ent = (o: object) => ({ id: 'e1', entity_type: 'EMAIL', original_entity_type: 'EMAIL', masked_value: 'r***@example.com', confidence_score: 0.97,
    start_position: TEXT.indexOf('riya'), end_position: TEXT.indexOf(' today'), page_number: null, detection_method: 'rule', review_status: 'PENDING', is_manual: false, ...o })
  const routes = (entities: object[]) => ({
    ...signedIn,
    'GET /scans/s1/text': () => ({ body: { text: TEXT, quality: { warnings: ['low OCR confidence'] } } }),
    'GET /scans/s1/entities': () => ({ body: { items: entities, total: entities.length, page: 1, page_size: 200, counts_by_status: { PENDING: entities.length } } }),
    'GET /scans/s1': () => ({ body: { id: 's1', document_id: 'd1', detection_mode: 'hybrid', model_version: 'v1', status: 'COMPLETED' } }),
    'GET /documents/d1/redacted': () => ({ body: [] }),
  })
  const open = () => mount(<RequireAuth><Routes><Route path="/scans/:id/review" element={<Review />} /></Routes></RequireAuth>, '/scans/s1/review')

  it('renders document text as plain text, never as HTML', async () => {
    mockApi(routes([ent({})]))
    const { container } = open()
    const mark = await screen.findByText('riya@example.com', { selector: 'mark' })
    expect(mark).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()                    // the <img> in the document was not parsed
    expect(screen.getByTestId('doc')).toHaveTextContent('<img src=x onerror=alert(1)>')
  })

  it('labels model suggestions vs human decisions and flags low confidence', async () => {
    mockApi(routes([ent({}), ent({ id: 'e2', masked_value: 'a***', review_status: 'REDACT', confidence_score: 0.4, start_position: 0, end_position: 7 })]))
    open()
    expect(await screen.findByText('model suggestion')).toBeInTheDocument()
    expect(screen.getByText('human decision')).toBeInTheDocument()
    expect(screen.getByText(/low confidence/)).toBeInTheDocument()
    expect(screen.getByText(/low OCR confidence/)).toBeInTheDocument()
  })

  it('sends the chosen decision for an entity', async () => {
    mockApi({ ...routes([ent({})]), 'POST /entities/e1/review': () => ({ body: {} }) })
    open()
    fireEvent.click(await screen.findByRole('button', { name: 'Redact' }))
    await waitFor(() => expect(calls.find((c) => c.path === '/entities/e1/review')).toBeTruthy())
    expect(JSON.parse(calls.find((c) => c.path === '/entities/e1/review')!.body!)).toEqual({ action: 'REDACT' })
  })

  it('asks for confirmation before redacting and warns about unreviewed items', async () => {
    mockApi({ ...routes([ent({ review_status: 'REDACT' })]), 'GET /scans/s1/entities': () => ({ body: { items: [ent({ review_status: 'REDACT' })], total: 1, page: 1, page_size: 200, counts_by_status: { REDACT: 1, PENDING: 3 } } }), 'POST /scans/s1/redact': () => ({ status: 201, body: { id: 'r1', validation_status: 'PASSED' } }) })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    open()
    const btn = await screen.findByRole('button', { name: 'Redact 1 item(s)' })
    fireEvent.click(btn)
    expect(confirmSpy.mock.calls[0][0]).toContain('3 item(s) are still unreviewed')
    expect(calls.some((c) => c.path === '/scans/s1/redact')).toBe(false)     // declined: nothing sent
    await waitFor(() => expect(btn).not.toBeDisabled())
    fireEvent.click(btn)
    expect(await screen.findByText('PASSED')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download redacted file' })).toBeInTheDocument()
  })

  it('shows no download button when verification failed', async () => {
    mockApi({ ...routes([ent({ review_status: 'REDACT' })]), 'GET /scans/s1/entities': () => ({ body: { items: [ent({ review_status: 'REDACT' })], total: 1, page: 1, page_size: 200, counts_by_status: { REDACT: 1 } } }), 'POST /scans/s1/redact': () => ({ status: 201, body: { id: 'r1', validation_status: 'FAILED' } }) })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    open()
    fireEvent.click(await screen.findByRole('button', { name: 'Redact 1 item(s)' }))
    expect(await screen.findByText(/did not pass verification/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Download redacted file' })).toBeNull()
  })

  it('shows the API error when the text cannot be loaded', async () => {
    mockApi({ ...routes([]), 'GET /scans/s1/text': () => ({ status: 404, body: { detail: 'scan not found' } }) })
    open()
    expect(await screen.findByRole('alert')).toHaveTextContent('scan not found')
  })
})

describe('segments()', () => {
  const e = (id: string, s: number, en: number) => ({ id, start_position: s, end_position: en }) as never
  it('splits text around non-overlapping entities and skips overlaps', () => {
    const out = segments('0123456789', [e('b', 6, 8), e('a', 2, 4), e('c', 3, 5)])
    expect(out.map((s) => [s.start, s.end, s.ent?.id])).toEqual([[0, 2, undefined], [2, 4, 'a'], [4, 6, undefined], [6, 8, 'b'], [8, 10, undefined]])
  })
  it('handles no entities and out-of-range entities', () => {
    expect(segments('abc', []).map((s) => [s.start, s.end])).toEqual([[0, 3]])
    expect(segments('abc', [e('x', 1, 99)]).map((s) => [s.start, s.end])).toEqual([[0, 3]])
  })
})
