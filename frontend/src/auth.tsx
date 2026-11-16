import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import { api, setToken, tryRestoreSession, type User } from './api'

interface AuthState {
  user: User | null
  loading: boolean
  login: (email: string, password: string) => Promise<void>
  register: (name: string, email: string, password: string) => Promise<void>
  logout: () => Promise<void>
}
const Ctx = createContext<AuthState>(null!)
export const useAuth = () => useContext(Ctx)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    tryRestoreSession().then(setUser).catch(() => setUser(null)).finally(() => setLoading(false))
  }, [])

  const login = async (email: string, password: string) => {
    const r = await api<{ access_token: string; user: User }>('/auth/login', { method: 'POST', json: { email, password } })
    setToken(r.access_token)
    setUser(r.user)
  }
  const register = async (name: string, email: string, password: string) => {
    await api('/auth/register', { method: 'POST', json: { name, email, password } })
    await login(email, password)
  }
  const logout = async () => {
    await api('/auth/logout', { method: 'POST' }).catch(() => undefined)
    setToken(null)
    setUser(null)
  }
  return <Ctx.Provider value={{ user, loading, login, register, logout }}>{children}</Ctx.Provider>
}

/** Redirects to /login when signed out; to / when the role is not allowed. The server enforces this too. */
export function RequireAuth({ children, roles }: { children: ReactNode; roles?: User['role'][] }) {
  const { user, loading } = useAuth()
  if (loading) return <p className="muted">Loading…</p>
  if (!user) return <Navigate to="/login" replace />
  if (roles && !roles.includes(user.role)) return <Navigate to="/dashboard" replace />
  return <>{children}</>
}
