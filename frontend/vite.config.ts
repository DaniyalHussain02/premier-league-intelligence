import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'
import { staleThresholdHours } from './src/lib/format'

export default defineConfig(() => ({
  plugins: [react()],
  envDir: false as const,
  define: {
    __FORECAST_STALE_HOURS__: JSON.stringify(staleThresholdHours(process.env.FORECAST_STALE_HOURS)),
  },
  test: {
    environment: 'jsdom',
  },
}))
