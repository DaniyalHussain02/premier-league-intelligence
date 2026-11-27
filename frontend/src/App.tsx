import { lazy, Suspense, useEffect, useState } from 'react'
import { Dashboard } from './pages/Dashboard'
import './styles/dashboard.css'

const ProbabilityHistory = lazy(() => import('./components/ProbabilityHistory'))
const Methodology = lazy(() => import('./pages/Methodology'))

function currentView() {
  if (location.hash === '#history') return 'history'
  if (location.hash === '#methodology') return 'methodology'
  return 'forecast'
}

export function App() {
  const [view, setView] = useState(currentView)

  useEffect(() => {
    const update = () => setView(currentView())
    window.addEventListener('hashchange', update)
    return () => window.removeEventListener('hashchange', update)
  }, [])

  return (
    <main className="dashboard">
      <h1>Premier League Intelligence Platform</h1>
      <nav aria-label="Forecast views">
        <a href="#forecast" aria-current={view === 'forecast' ? 'page' : undefined}>Current forecast</a>
        {' · '}
        <a href="#history" aria-current={view === 'history' ? 'page' : undefined}>Forecast history</a>
        {' · '}
        <a href="#methodology" aria-current={view === 'methodology' ? 'page' : undefined}>Methodology</a>
      </nav>
      <Suspense fallback={<p role="status">Loading view…</p>}>
        {view === 'forecast' && <Dashboard />}
        {view === 'history' && <ProbabilityHistory />}
        {view === 'methodology' && <Methodology />}
      </Suspense>
    </main>
  )
}
