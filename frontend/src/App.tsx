import { NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from './api'
import { RequireAuth, useAuth } from './auth'
import Account from './pages/Account'
import Admin from './pages/Admin'
import Dashboard from './pages/Dashboard'
import DocumentDetail from './pages/DocumentDetail'
import Documents from './pages/Documents'
import { AuthPage, Landing } from './pages/Public'
import Review from './pages/Review'
import Upload from './pages/Upload'

function Nav() {
  const { user, logout } = useAuth()
  const nav = useNavigate()
  // Fetch whether registration is open so we can hide the link when it's closed.
  const { data: authConfig } = useQuery({
    queryKey: ['auth-config'],
    queryFn: () => api<{ registration_open: boolean }>('/auth/config'),
    staleTime: 60_000,
  })
  const registrationOpen = authConfig?.registration_open ?? true

  return (
    <nav className="nav" aria-label="Main">
      <b>PII Sentinel</b>
      {user ? (
        <>
          <NavLink to="/dashboard">Dashboard</NavLink>
          <NavLink to="/documents">Documents</NavLink>
          <NavLink to="/upload">Upload</NavLink>
          {user.role === 'ADMIN' && <NavLink to="/admin">Admin</NavLink>}
          <span className="grow" />
          <NavLink to="/account" className="muted">{user.name} ({user.role.toLowerCase()})</NavLink>
          <button onClick={async () => { nav('/'); await logout() }}>Log out</button>
        </>
      ) : (
        <>
          <span className="grow" />
          <NavLink to="/login">Log in</NavLink>
          {registrationOpen && <NavLink to="/register">Register</NavLink>}
        </>
      )}
    </nav>
  )
}

export default function App() {
  const guard = (el: React.ReactNode, roles?: ('USER' | 'REVIEWER' | 'ADMIN')[]) => <RequireAuth roles={roles}>{el}</RequireAuth>
  return (
    <>
      <Nav />
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/login" element={<AuthPage mode="login" />} />
        <Route path="/register" element={<AuthPage mode="register" />} />
        <Route path="/dashboard" element={guard(<Dashboard />)} />
        <Route path="/documents" element={guard(<Documents />)} />
        <Route path="/documents/:id" element={guard(<DocumentDetail />)} />
        <Route path="/upload" element={guard(<Upload />)} />
        <Route path="/scans/:id/review" element={guard(<Review />)} />
        <Route path="/account" element={guard(<Account />)} />
        <Route path="/admin" element={guard(<Admin />, ['ADMIN'])} />
        <Route path="*" element={<div className="wrap"><h1>Page not found</h1></div>} />
      </Routes>
    </>
  )
}
