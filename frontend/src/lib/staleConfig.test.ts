// @vitest-environment node
import { resolveConfig } from 'vite'
import { afterEach, expect, test, vi } from 'vitest'

afterEach(() => vi.unstubAllEnvs())

test.each([
  ['12.5', '12.5'],
  [undefined, '168'],
  ['invalid-config-marker', '168'],
  ['0', '168'],
])('embeds only the validated stale threshold for %s', async (value, expected) => {
  vi.stubEnv('FORECAST_STALE_HOURS', value)
  vi.stubEnv('VITE_FORECAST_STALE_HOURS', '99')
  vi.stubEnv('UNRELATED_BUILD_SETTING', 'unrelated-test-marker')

  const config = await resolveConfig({ logLevel: 'silent' }, 'build', 'production', 'production')
  expect(config.define).toEqual({ __FORECAST_STALE_HOURS__: expected })
  expect(config.env).not.toHaveProperty('UNRELATED_BUILD_SETTING')
})
