const defaultStaleHours = 168
const hourMs = 60 * 60 * 1000
const utcTime = new Intl.DateTimeFormat('en-GB', {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: 'UTC',
})
const wholeNumber = new Intl.NumberFormat('en-GB')
const decimalNumber = new Intl.NumberFormat('en-GB', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
})
const percent = new Intl.NumberFormat('en-GB', {
  style: 'percent',
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
})

export function formatUtc(timestamp: string): string {
  return `${utcTime.format(new Date(timestamp))} UTC`
}

export function formatCount(value: number): string {
  return wholeNumber.format(value)
}

export function formatProjectedPoints(value: number): string {
  return decimalNumber.format(value)
}

export function formatProbability(value: number): string {
  const rounded = percent.format(value)
  if (value > 0 && rounded === percent.format(0)) return '<0.1%'
  if (value < 1 && rounded === percent.format(1)) return '>99.9%'
  return rounded
}

export function staleThresholdHours(value: unknown): number {
  if (typeof value !== 'string' || !/^\d+(?:\.\d+)?$/.test(value.trim())) {
    return defaultStaleHours
  }
  const hours = Number(value)
  return Number.isFinite(hours) && hours > 0 ? hours : defaultStaleHours
}

export function isStale(generatedAt: string, threshold: number, now = Date.now()): boolean {
  return (now - Date.parse(generatedAt)) / hourMs >= threshold
}
