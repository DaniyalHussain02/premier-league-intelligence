import { useEffect, useState } from 'react'
import { ForecastTable } from '../components/ForecastTable'
import { StatusBanner } from '../components/StatusBanner'
import { loadLatestManifest, loadLatestSnapshot, loadSeasonIndex } from '../lib/dataClient'
import { formatCount, formatUtc, isStale } from '../lib/format'
import type { ForecastSnapshot, SeasonIndex } from '../lib/types'

type DashboardState =
  | { status: 'loading' }
  | { status: 'unavailable' | 'waiting'; season?: SeasonIndex['season_status'] }
  | { status: 'ready'; season: SeasonIndex['season_status']; snapshot: ForecastSnapshot; fallback: boolean }

export function Dashboard() {
  const [request, setRequest] = useState(0)
  const [state, setState] = useState<DashboardState>({ status: 'loading' })

  useEffect(() => {
    let current = true

    async function load() {
      const index = await loadSeasonIndex()
      if (!current) return
      if (index.status !== 'ok') {
        setState({ status: 'unavailable' })
        return
      }
      const season = index.data.season_status
      if (index.data.display_season_id === null) {
        setState({ status: 'waiting', season })
        return
      }
      const manifest = await loadLatestManifest(index.data)
      if (!current) return
      if (manifest.status !== 'ok') {
        setState({ status: 'unavailable', season })
        return
      }
      const snapshot = await loadLatestSnapshot(manifest.data)
      if (!current) return
      setState(snapshot.status === 'unavailable'
        ? { status: 'unavailable', season }
        : { status: 'ready', season, snapshot: snapshot.data, fallback: snapshot.status === 'fallback' })
    }

    void load().catch(() => {
      if (current) setState({ status: 'unavailable' })
    })
    return () => { current = false }
  }, [request])

  function retry() {
    setState({ status: 'loading' })
    setRequest((previous) => previous + 1)
  }

  const seasonStatus = state.status === 'loading' ? undefined : state.season
  const snapshot = state.status === 'ready' ? state.snapshot : undefined

  return (
    <section aria-label="Current forecast" aria-busy={state.status === 'loading'}>
      {state.status === 'loading' && <StatusBanner>Loading forecast…</StatusBanner>}
      {seasonStatus === 'pending' && (
        <StatusBanner>
          The next season is pending confirmation.{snapshot && ' Showing the last available season.'}
        </StatusBanner>
      )}
      {seasonStatus === 'blocked' && (
        <StatusBanner>
          The active season could not be confirmed.{snapshot && ' Showing the last available season.'}
        </StatusBanner>
      )}
      {state.status === 'unavailable' && (
        <StatusBanner onRetry={retry}>Forecast data is unavailable. Please try again.</StatusBanner>
      )}
      {state.status === 'waiting' && <StatusBanner>No forecast is available to display yet.</StatusBanner>}
      {state.status === 'ready' && state.fallback && (
        <StatusBanner>The latest snapshot could not be loaded. Showing the last loaded forecast for this season.</StatusBanner>
      )}
      {snapshot && (
        <>
          {isStale(snapshot.generated_at, __FORECAST_STALE_HOURS__) && (
            <StatusBanner>This forecast is stale based on its generation time.</StatusBanner>
          )}
          <dl className="forecast-metadata">
            <div><dt>Season</dt><dd>{snapshot.season_id}</dd></div>
            <div><dt>Generated</dt><dd><time dateTime={snapshot.generated_at}>{formatUtc(snapshot.generated_at)}</time></dd></div>
            <div><dt>Data cutoff</dt><dd><time dateTime={snapshot.data_cutoff_at}>{formatUtc(snapshot.data_cutoff_at)}</time></dd></div>
            <div><dt>Simulations</dt><dd>{formatCount(snapshot.simulation_count)}</dd></div>
          </dl>
          <ForecastTable teams={snapshot.teams} />
        </>
      )}
    </section>
  )
}
