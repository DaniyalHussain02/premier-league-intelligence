import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { App } from './App'

vi.mock('./lib/dataClient', () => ({
  loadSeasonIndex: vi.fn(() => Promise.resolve({ status: 'unavailable' })),
  loadLatestManifest: vi.fn(),
  loadLatestSnapshot: vi.fn(),
}))

afterEach(cleanup)

test('renders the dashboard heading', async () => {
  render(<App />)
  expect(screen.getByRole('heading', {
    level: 1,
    name: 'Premier League Intelligence Platform',
  })).toBeTruthy()
  expect(await screen.findByText(/forecast data is unavailable/i)).toBeTruthy()
})
