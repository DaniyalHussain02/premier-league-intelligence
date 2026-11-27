import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { loadLatestManifest, loadLatestSnapshot, loadSeasonIndex } from '../lib/dataClient'
import { formatProbability, isStale, staleThresholdHours } from '../lib/format'
import type { ForecastSnapshot, LatestManifest, SeasonIndex } from '../lib/types'
import { Dashboard } from './Dashboard'

vi.mock('../lib/dataClient', () => ({
  loadSeasonIndex: vi.fn(),
  loadLatestManifest: vi.fn(),
  loadLatestSnapshot: vi.fn(),
}))

const index: SeasonIndex = {
  schema_version: 1,
  active_season_id: '2026-2027',
  display_season_id: '2026-2027',
  season_status: 'active',
  latest_manifest_path: '/data/seasons/2026-2027/latest.json',
}
const manifest: LatestManifest = {
  schema_version: 1,
  season_id: '2026-2027',
  snapshot_id: 'mock',
  snapshot_path: '/data/seasons/2026-2027/snapshots/mock.json',
  input_hash: 'a'.repeat(64),
  model_version: 'mock-v1',
  generated_at: '2026-11-20T12:00:00Z',
  data_cutoff_at: '2026-11-19T21:00:00Z',
}
const snapshot: ForecastSnapshot = {
  schema_version: 1,
  season_id: manifest.season_id,
  snapshot_id: manifest.snapshot_id,
  model_version: manifest.model_version,
  generated_at: manifest.generated_at,
  data_cutoff_at: manifest.data_cutoff_at,
  simulation_count: 10_000,
  representative_table_method: 'maximum-weight-position-assignment',
  teams: Array.from({ length: 20 }, (_, i) => ({
    team_id: `club-${i + 1}`,
    name: `Club ${i + 1}`,
    actual_rank: i <= 1 ? 1 : i + 1,
    played: i % 2 ? 10 : 11,
    actual_points: 25,
    expected_points: 50.2 + i,
    predicted_rank: i + 1,
    title_probability: i === 0 ? 0.9996 : i === 1 ? 0.0004 : 0,
    relegation_probability: i >= 17 ? 1 : 0,
    position_probabilities: Array.from({ length: 20 }, (_, rank) =>
      i < 2 && rank < 2 ? (i === rank ? 0.9996 : 0.0004) : Number(rank === i)),
  })).reverse(),
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-25T12:00:00Z'))
  vi.mocked(loadSeasonIndex).mockResolvedValue({ status: 'ok', data: index })
  vi.mocked(loadLatestManifest).mockResolvedValue({ status: 'ok', data: manifest })
  vi.mocked(loadLatestSnapshot).mockResolvedValue({ status: 'ok', data: snapshot })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

test('renders all validated clubs, metadata and semantic headers in assigned order', async () => {
  render(<Dashboard />)
  const table = await screen.findByRole('table')
  expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual([
    'Predicted rank', 'Club', 'Actual rank', 'Actual points', 'Games played',
    'Projected points', 'Title probability', 'Relegation probability',
  ])
  const rows = within(table).getAllByRole('row').slice(1)
  expect(rows).toHaveLength(20)
  expect(within(rows[0]).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
    '1', '1', '25', '11', '50.2', '>99.9%', '0.0%',
  ])
  expect(within(rows[1]).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
    '2', '1', '25', '10', '51.2', '<0.1%', '0.0%',
  ])
  expect(within(rows[19]).getByRole('rowheader').textContent).toBe('Club 20')
  expect(screen.getByRole('region', { name: 'Representative predicted table' }).tabIndex).toBe(0)
  expect(screen.getByText('20 Nov 2026, 12:00 UTC').getAttribute('datetime')).toBe(manifest.generated_at)
  expect(screen.getByText('19 Nov 2026, 21:00 UTC').getAttribute('datetime')).toBe(manifest.data_cutoff_at)
  expect(screen.getByText('10,000')).toBeTruthy()
  expect(screen.getByText(index.display_season_id!)).toBeTruthy()
  expect(screen.getByText(/clubs can compete for the same spot/i)).toBeTruthy()
})

test('shows loading and supports retry after data is unavailable', async () => {
  vi.mocked(loadSeasonIndex).mockResolvedValueOnce({ status: 'unavailable' })
  render(<Dashboard />)
  expect(screen.getByText('Loading forecast…')).toBeTruthy()
  const retry = await screen.findByRole('button', { name: /retry/i })
  expect(screen.queryByRole('table')).toBeNull()
  fireEvent.click(retry)
  expect(await screen.findByRole('table')).toBeTruthy()
})

test.each(['pending', 'blocked'] as const)('shows the last valid season when the index is %s', async (season_status) => {
  vi.mocked(loadSeasonIndex).mockResolvedValue({
    status: 'ok', data: { ...index, active_season_id: null, season_status },
  })
  render(<Dashboard />)
  await screen.findByRole('table')
  expect(screen.getByText(/showing the last available season/i)).toBeTruthy()
  expect(screen.queryByText(/showing the last loaded forecast/i)).toBeNull()
})

test('pending season without a display season makes no forecast request', async () => {
  vi.mocked(loadSeasonIndex).mockResolvedValue({
    status: 'ok',
    data: { ...index, active_season_id: null, display_season_id: null, latest_manifest_path: null, season_status: 'pending' },
  })
  render(<Dashboard />)
  expect(await screen.findByText(/pending confirmation/i)).toBeTruthy()
  expect(loadLatestManifest).not.toHaveBeenCalled()
  expect(screen.queryByRole('table')).toBeNull()
})

test('cached fallback preserves original timestamps and reports age separately', async () => {
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-29T12:00:00Z'))
  vi.mocked(loadLatestManifest).mockResolvedValue({
    status: 'ok', data: { ...manifest, generated_at: '2026-11-28T12:00:00Z' },
  })
  vi.mocked(loadLatestSnapshot).mockResolvedValue({ status: 'fallback', data: snapshot })
  render(<Dashboard />)
  expect(await screen.findByText(/showing the last loaded forecast/i)).toBeTruthy()
  expect(screen.getByText(/stale based on its generation time/i)).toBeTruthy()
  expect(screen.getByText('20 Nov 2026, 12:00 UTC').getAttribute('datetime')).toBe(snapshot.generated_at)
  expect(screen.getByText('19 Nov 2026, 21:00 UTC').getAttribute('datetime')).toBe(snapshot.data_cutoff_at)
  expect(screen.queryByText(/28 Nov/)).toBeNull()
})

test('age warning also applies to a successfully loaded old snapshot', async () => {
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-29T12:00:00Z'))
  render(<Dashboard />)
  expect(await screen.findByText(/stale based on its generation time/i)).toBeTruthy()
  expect(screen.queryByText(/showing the last loaded forecast/i)).toBeNull()
})

test.each(['manifest', 'snapshot'] as const)('shows unavailable when the %s could not be loaded', async (stage) => {
  const load = stage === 'manifest' ? loadLatestManifest : loadLatestSnapshot
  vi.mocked(load).mockResolvedValue({ status: 'unavailable' })
  render(<Dashboard />)
  expect(await screen.findByText(/forecast data is unavailable/i)).toBeTruthy()
  expect(screen.queryByRole('table')).toBeNull()
})

test('a superseded request resolving after the current request cannot overwrite it', async () => {
  let complete!: (result: Awaited<ReturnType<typeof loadSeasonIndex>>) => void
  vi.mocked(loadSeasonIndex).mockReturnValueOnce(new Promise((resolve) => { complete = resolve }))
  render(<StrictMode><Dashboard /></StrictMode>)
  expect(await screen.findByRole('table')).toBeTruthy()
  await act(async () => complete({ status: 'unavailable' }))
  expect(screen.queryByText(/forecast data is unavailable/i)).toBeNull()
  expect(screen.getByRole('table')).toBeTruthy()
})

test('validates stale configuration and warns at the exact age threshold', () => {
  for (const value of [undefined, '', '  ', '0', '-50', 'Infinity', '0x10', 'abc', '9'.repeat(400)]) {
    expect(staleThresholdHours(value)).toBe(168)
  }
  expect(staleThresholdHours(' 12.5 ')).toBe(12.5)
  const boundary = Date.parse(snapshot.generated_at) + 168 * 3_600_000
  expect(isStale(snapshot.generated_at, 168, boundary - 1)).toBe(false)
  expect(isStale(snapshot.generated_at, 168, boundary)).toBe(true)
  expect(isStale(snapshot.generated_at, 168, boundary - 169 * 3_600_000)).toBe(false)
})

test('distinguishes certain and impossible probabilities from rounded extremes', () => {
  expect([0, 0.00001, 0.4321, 0.99999, 1].map(formatProbability)).toEqual([
    '0.0%', '<0.1%', '43.2%', '>99.9%', '100.0%',
  ])
})
