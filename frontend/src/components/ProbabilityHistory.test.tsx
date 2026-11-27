import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { loadHistory, loadHistorySnapshot, loadSeasonIndex } from '../lib/dataClient'
import type { ForecastSnapshot, HistoryIndex, SeasonIndex } from '../lib/types'
import ProbabilityHistory from './ProbabilityHistory'

vi.mock('../lib/dataClient', () => ({
  loadSeasonIndex: vi.fn(),
  loadHistory: vi.fn(),
  loadHistorySnapshot: vi.fn(),
}))

const season: SeasonIndex = {
  schema_version: 1,
  active_season_id: '2026-2027',
  display_season_id: '2026-2027',
  season_status: 'active',
  latest_manifest_path: '/data/seasons/2026-2027/latest.json',
}
const history: HistoryIndex = {
  schema_version: 1,
  season_id: '2026-2027',
  snapshots: ['first', 'middle', 'last'].map((id, i) => ({
    snapshot_id: id,
    generated_at: `2026-11-${10 + i}T12:00:00Z`,
    data_cutoff_at: `2026-11-${10 + i}T08:00:00Z`,
    matchweek_label: `Matchweek ${i + 1}`,
    path: `/data/seasons/2026-2027/snapshots/${id}.json`,
  })),
}

function snapshot(id: string): ForecastSnapshot {
  const entry = history.snapshots.find((entry) => entry.snapshot_id === id)!
  return {
    schema_version: 1,
    snapshot_id: id,
    season_id: history.season_id,
    generated_at: entry.generated_at,
    data_cutoff_at: entry.data_cutoff_at,
    model_version: 'mock',
    simulation_count: 1000,
    representative_table_method: 'mock',
    teams: ['Alpha', 'Beta'].map((name, i) => ({
      team_id: name.toLowerCase(), name,
      actual_rank: i + 1, played: 12, actual_points: 30,
      expected_points: 50, predicted_rank: i + 1,
      title_probability: id === 'first' ? 0.25 : 0.5, relegation_probability: i === 0 ? 0 : 0.0001,
      position_probabilities: [],
    })),
  }
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(loadSeasonIndex).mockResolvedValue({ status: 'ok', data: season })
  vi.mocked(loadHistory).mockResolvedValue({ status: 'ok', data: history })
  vi.mocked(loadHistorySnapshot).mockImplementation(async (_, id) => ({ status: 'ok', data: snapshot(id) }))
})

afterEach(cleanup)

test.each([0, 1])('shows the empty history message for %i snapshots', async (count) => {
  vi.mocked(loadHistory).mockResolvedValue({
    status: 'ok', data: { ...history, snapshots: history.snapshots.slice(0, count) },
  })
  render(<ProbabilityHistory />)
  expect(await screen.findByText('History will appear after the next update.')).toBeTruthy()
  expect(loadHistorySnapshot).not.toHaveBeenCalled()
})

test('loads only selected snapshots and compares one club with original dates', async () => {
  render(<ProbabilityHistory />)
  const table = await screen.findByRole('table', { name: /alpha/i })
  expect(vi.mocked(loadHistorySnapshot).mock.calls.map(([, id]) => id)).toEqual(['first', 'last'])
  expect(within(table).getByText('25.0%')).toBeTruthy()
  expect(within(table).getByText('50.0%')).toBeTruthy()
  expect(within(table).getByText('10 Nov 2026, 08:00 UTC').getAttribute('datetime')).toBe(history.snapshots[0].data_cutoff_at)
  fireEvent.change(screen.getByLabelText('Club'), { target: { value: 'beta' } })
  expect(screen.getByRole('table', { name: /beta/i })).toBeTruthy()
  expect(screen.getAllByText('<0.1%')).toHaveLength(2)
  expect(loadHistorySnapshot).toHaveBeenCalledTimes(2)
  fireEvent.change(screen.getByLabelText('First snapshot'), { target: { value: 'middle' } })
  expect(await screen.findByRole('table', { name: /beta/i })).toBeTruthy()
  expect(screen.getByRole('rowheader', { name: /matchweek 2/i })).toBeTruthy()
  expect(loadHistorySnapshot).toHaveBeenCalledWith(history, 'middle')
})

test('an older comparison cannot overwrite the current snapshot selection', async () => {
  let resolve!: (result: Awaited<ReturnType<typeof loadHistorySnapshot>>) => void
  vi.mocked(loadHistorySnapshot).mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  render(<ProbabilityHistory />)
  await screen.findByText('Loading selected snapshots…')
  fireEvent.change(screen.getByLabelText('First snapshot'), { target: { value: 'middle' } })
  const table = await screen.findByRole('table')
  await act(async () => resolve({ status: 'ok', data: snapshot('first') }))
  expect(within(table).getByRole('rowheader', { name: /matchweek 2/i })).toBeTruthy()
  expect(screen.queryByRole('rowheader', { name: /matchweek 1/i })).toBeNull()
})

test('keeps unavailable history distinct from an empty verified index', async () => {
  vi.mocked(loadHistory).mockResolvedValue({ status: 'unavailable' })
  render(<ProbabilityHistory />)
  expect(await screen.findByText('Forecast history is unavailable.')).toBeTruthy()
  expect(loadHistorySnapshot).not.toHaveBeenCalled()
})

test('does not present partial data if a selected snapshot is unavailable', async () => {
  vi.mocked(loadHistorySnapshot).mockResolvedValueOnce({ status: 'unavailable' })
  render(<ProbabilityHistory />)
  expect(await screen.findByText('The selected snapshots are unavailable.')).toBeTruthy()
  expect(screen.queryByRole('table')).toBeNull()
})
