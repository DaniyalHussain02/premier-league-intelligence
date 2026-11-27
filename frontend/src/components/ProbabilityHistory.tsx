import { useEffect, useState } from 'react'
import { loadHistory, loadHistorySnapshot, loadSeasonIndex } from '../lib/dataClient'
import { formatProbability, formatUtc } from '../lib/format'
import type { ForecastSnapshot, HistoryEntry, HistoryIndex } from '../lib/types'
import '../styles/history.css'

type HistoryState = { status: 'loading' | 'unavailable' | 'empty' }
  | { status: 'ready'; index: HistoryIndex }

type ComparisonState =
  | { status: 'loading' | 'unavailable' }
  | { status: 'ready'; snapshots: [ForecastSnapshot, ForecastSnapshot] }

function snapshotLabel(entry: HistoryEntry) {
  return `${entry.matchweek_label} · ${formatUtc(entry.generated_at)}`
}

function Comparison({
  index, firstEntry, secondEntry, club, setClub,
}: {
  index: HistoryIndex
  firstEntry: HistoryEntry
  secondEntry: HistoryEntry
  club: string
  setClub: (id: string) => void
}) {
  const [state, setState] = useState<ComparisonState>({ status: 'loading' })

  useEffect(() => {
    let active = true
    async function load() {
      const results = await Promise.all([
        loadHistorySnapshot(index, firstEntry.snapshot_id),
        loadHistorySnapshot(index, secondEntry.snapshot_id),
      ])
      if (!active) return
      if (results[0].status === 'ok' && results[1].status === 'ok') {
        setState({ status: 'ready', snapshots: [results[0].data, results[1].data] })
      } else {
        setState({ status: 'unavailable' })
      }
    }
    void load().catch(() => { if (active) setState({ status: 'unavailable' }) })
    return () => { active = false }
  }, [index, firstEntry, secondEntry])

  if (state.status !== 'ready') {
    return <p role="status">{state.status === 'loading'
      ? 'Loading selected snapshots…' : 'The selected snapshots are unavailable.'}</p>
  }

  const [first, second] = state.snapshots
  const clubs = new Map(first.teams.map((team) => [team.team_id, team.name]))
  // T12 validates unique canonical IDs within each snapshot.
  if (clubs.size !== second.teams.length
    || second.teams.some((team) => clubs.get(team.team_id) !== team.name)) {
    return <p role="status">The selected snapshots are unavailable for comparison because their club rosters differ.</p>
  }
  const choices = [...clubs].sort((left, right) => left[1].localeCompare(right[1]))
  const selected = clubs.has(club) ? club : choices[0][0]

  return (
    <>
      <label className="history-control">
        Club
        <select value={selected} onChange={(event) => setClub(event.target.value)}>
          {choices.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select>
      </label>
      <div className="table-scroll history-table" role="region" aria-label="Club probability comparison" tabIndex={0}>
        <table>
          <caption>{clubs.get(selected)} · Published forecast comparison</caption>
          <thead><tr>
            <th scope="col">Snapshot</th>
            <th scope="col">Data cutoff</th>
            <th scope="col">Title probability</th>
            <th scope="col">Relegation probability</th>
          </tr></thead>
          <tbody>
            {state.snapshots.map((snapshot, i) => {
              const team = snapshot.teams.find((team) => team.team_id === selected)
              return (
                <tr key={snapshot.snapshot_id}>
                  <th scope="row">{snapshotLabel(i === 0 ? firstEntry : secondEntry)}</th>
                  <td><time dateTime={snapshot.data_cutoff_at}>{formatUtc(snapshot.data_cutoff_at)}</time></td>
                  <td>{team ? formatProbability(team.title_probability) : 'Unavailable'}</td>
                  <td>{team ? formatProbability(team.relegation_probability) : 'Unavailable'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

function SnapshotSelector({ index }: { index: HistoryIndex }) {
  const snapshots = index.snapshots
  const [first, setFirst] = useState(snapshots[0].snapshot_id)
  const [second, setSecond] = useState(snapshots[snapshots.length - 1].snapshot_id)
  const [club, setClub] = useState('')

  const firstEntry = snapshots.find((entry) => entry.snapshot_id === first)!
  const secondEntry = snapshots.find((entry) => entry.snapshot_id === second)!
  const options = snapshots.map((entry) => (
    <option key={entry.snapshot_id} value={entry.snapshot_id}>{snapshotLabel(entry)}</option>
  ))

  return (
    <>
      <p>Season {index.season_id}</p>
      <div className="history-selectors">
        <label className="history-control">
          First snapshot
          <select value={first} onChange={(event) => setFirst(event.target.value)}>{options}</select>
        </label>
        <label className="history-control">
          Second snapshot
          <select value={second} onChange={(event) => setSecond(event.target.value)}>{options}</select>
        </label>
      </div>
      {first === second
        ? <p role="status">Choose two different snapshots to compare.</p>
        : <Comparison key={`${first}:${second}`} index={index} firstEntry={firstEntry} secondEntry={secondEntry} club={club} setClub={setClub} />}
    </>
  )
}

export default function ProbabilityHistory() {
  const [state, setState] = useState<HistoryState>({ status: 'loading' })

  useEffect(() => {
    let active = true
    async function load() {
      const season = await loadSeasonIndex()
      if (!active) return
      if (season.status !== 'ok' || season.data.display_season_id === null) {
        setState({ status: season.status === 'ok' ? 'empty' : 'unavailable' })
        return
      }
      const result = await loadHistory(season.data)
      if (!active) return
      if (result.status !== 'ok') setState({ status: 'unavailable' })
      else if (result.data.snapshots.length < 2) setState({ status: 'empty' })
      else setState({ status: 'ready', index: result.data })
    }
    void load().catch(() => { if (active) setState({ status: 'unavailable' }) })
    return () => { active = false }
  }, [])

  return (
    <section aria-labelledby="history-heading">
      <h2 id="history-heading">Forecast history</h2>
      <p>Compare title and relegation probabilities from forecasts published since launch.</p>
      {state.status === 'loading' && <p role="status">Loading forecast history…</p>}
      {state.status === 'unavailable' && <p role="status">Forecast history is unavailable.</p>}
      {state.status === 'empty' && <p role="status">History will appear after the next update.</p>}
      {state.status === 'ready' && <SnapshotSelector index={state.index} />}
    </section>
  )
}
