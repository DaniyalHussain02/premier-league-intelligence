import type { ReactNode } from 'react'

interface StatusBannerProps {
  children: ReactNode
  onRetry?: () => void
}

export function StatusBanner({ children, onRetry }: StatusBannerProps) {
  return (
    <div className="status-banner">
      <p role="status">{children}</p>
      {onRetry && <button type="button" onClick={onRetry}>Retry loading forecast</button>}
    </div>
  )
}
