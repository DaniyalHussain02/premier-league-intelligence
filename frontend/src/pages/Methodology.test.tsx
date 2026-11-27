import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { loadBacktest, loadBacktestIndex } from '../lib/dataClient'
import type { BacktestIndex, BacktestSummary } from '../lib/types'
import Methodology from './Methodology'

vi.mock('../lib/dataClient', () => ({ loadBacktestIndex: vi.fn(), loadBacktest: vi.fn() }))

const report: BacktestSummary = {
  schema_version: 1,
  season_id: '2024-2025',
  model_version: 'verified-test',
  training_period: ['2023-08-01T00:00:00Z', '2024-08-01T00:00:00Z'],
  cutoff_policy: 'One hour before kickoff',
  sample_size: 380,
  baseline: 'Empirical outcome frequencies',
  multiclass_brier_model: 0.592,
  multiclass_brier_baseline: 0.624,
  limitations: ['Single-season sample'],
}
const index: BacktestIndex = {
  schema_version: 1,
  reports: [report.season_id, '2023-2024'].map((season, i) => ({
    season_id: season, backtest_id: `report-${i}`, model_version: report.model_version,
    path: `/data/backtests/report-${i}.json`,
  })),
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(loadBacktestIndex).mockResolvedValue({ status: 'ok', data: index })
  vi.mocked(loadBacktest).mockImplementation(async (_, id) => ({
    status: 'ok', data: id === 'report-0' ? report : { ...report, season_id: '2023-2024' },
  }))
})
afterEach(cleanup)

test('loads selected retrospective reports and displays scores, provenance, and limitations', async () => {
  render(<Methodology />)
  expect(await screen.findByText('380 matches')).toBeTruthy()
  for (const value of ['verified-test', 'One hour before kickoff', 'Empirical outcome frequencies',
    '0.592', '0.624', 'Single-season sample', '1 Aug 2023, 00:00 UTC', '1 Aug 2024, 00:00 UTC']) {
    expect(screen.getByText(value, { exact: true })).toBeTruthy()
  }
  expect(screen.getByRole('region', { name: 'Retrospective evaluation' })).toBeTruthy()
  expect(loadBacktest).toHaveBeenCalledTimes(1)
  fireEvent.change(screen.getByLabelText('Retrospective report'), { target: { value: 'report-1' } })
  expect(await screen.findByText('2023-2024', { exact: true })).toBeTruthy()
  expect(loadBacktest).toHaveBeenLastCalledWith(index, 'report-1')
})

test('shows an honest empty state and loads no report when the index is empty', async () => {
  vi.mocked(loadBacktestIndex).mockResolvedValue({ status: 'ok', data: { schema_version: 1, reports: [] } })
  render(<Methodology />)
  expect(await screen.findByText('No verified retrospective report is available.')).toBeTruthy()
  expect(loadBacktest).not.toHaveBeenCalled()
  expect(screen.queryByText('0.592')).toBeNull()
})

test('does not display scores for an unavailable indexed retrospective report', async () => {
  vi.mocked(loadBacktest).mockResolvedValue({ status: 'unavailable' })
  render(<Methodology />)
  expect(await screen.findByText('This retrospective report is unavailable.')).toBeTruthy()
  expect(screen.queryByText('0.592')).toBeNull()
})
