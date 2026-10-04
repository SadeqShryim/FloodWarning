import { lazy, Suspense } from 'react'

// Each screen is its own chunk, so the phone page never downloads the map code.
const ReportPage = lazy(() => import('./report/ReportPage'))
const DashboardPage = lazy(() => import('./dashboard/DashboardPage'))

type Route = 'report' | 'dashboard' | 'missing'

function currentRoute(): Route {
  const path = window.location.pathname.replace(/\/+$/, '') || '/'
  if (path === '/report') return 'report'
  if (path === '/' || path === '/dashboard') return 'dashboard'
  return 'missing'
}

export default function App() {
  const route = currentRoute()

  if (route === 'missing') {
    return (
      <main className="fl-missing">
        <h1>Page not found</h1>
        <p>
          <a href="/report">Report a flood</a> · <a href="/dashboard">Responder dashboard</a>
        </p>
      </main>
    )
  }

  return (
    <Suspense fallback={<div className="fl-boot">FloodLine</div>}>
      {route === 'report' ? <ReportPage /> : <DashboardPage />}
    </Suspense>
  )
}
