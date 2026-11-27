import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { App } from './App'
import { loadBacktestIndex, loadHistory } from './lib/dataClient'

vi.mock('./lib/dataClient', () => ({
  loadSeasonIndex: vi.fn(() => Promise.resolve({ status: 'unavailable' })),
  loadLatestManifest: vi.fn(),
  loadLatestSnapshot: vi.fn(),
  loadHistory: vi.fn(),
  loadBacktestIndex: vi.fn(() => Promise.resolve({ status: 'unavailable' })),
}))

afterEach(() => {
  cleanup()
  history.replaceState(null, '', '/')
  vi.clearAllMocks()
})

test('renders the dashboard heading', async () => {
  render(<App />)
  expect(screen.getByRole('heading', {
    level: 1,
    name: 'Premier League Intelligence Platform',
  })).toBeTruthy()
  expect(await screen.findByText(/forecast data is unavailable/i)).toBeTruthy()
})

test('loads forecast history and retrospective evaluation only when their views open', async () => {
  render(<App />)
  expect(loadHistory).not.toHaveBeenCalled()
  expect(loadBacktestIndex).not.toHaveBeenCalled()
  const navigate = (hash: string) => act(() => {
    history.replaceState(null, '', hash)
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  })
  navigate('#history')
  expect(await screen.findByRole('heading', { name: 'Forecast history' })).toBeTruthy()
  expect(loadBacktestIndex).not.toHaveBeenCalled()
  navigate('#methodology')
  expect(await screen.findByRole('heading', { name: 'Retrospective evaluation' })).toBeTruthy()
  expect(await screen.findByText('Retrospective reports could not be loaded.')).toBeTruthy()
  expect(screen.queryByRole('heading', { name: 'Forecast history' })).toBeNull()
})
