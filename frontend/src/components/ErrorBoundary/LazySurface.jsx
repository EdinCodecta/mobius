import { Suspense } from 'react'
import ErrorBoundary from './ErrorBoundary.jsx'

/**
 * A surface whose code loads on first render (`React.lazy`).
 *
 * Downloading a chunk can fail — most often because a deploy replaced the
 * build the running page came from. Each lazy surface therefore owns a
 * boundary of its own: the shared ErrorBoundary reloads onto the newer build
 * when one exists, and otherwise shows its recovery panel in place of this
 * surface only, instead of the failure reaching the shell-wide boundary.
 *
 * Props:
 *   label, recoveryKey — name the surface for the crash record and recovery
 *                        ledger (see ErrorBoundary)
 *   fallback           — shown while the chunk downloads; null by default
 *   variant            — ErrorBoundary variant; 'inline' fills the nearest
 *                        positioned ancestor, so surrounding chrome survives
 */
export default function LazySurface({
  label,
  recoveryKey,
  fallback = null,
  variant = 'inline',
  children,
}) {
  return (
    <ErrorBoundary variant={variant} label={label} recoveryKey={recoveryKey || label}>
      <Suspense fallback={fallback}>
        {children}
      </Suspense>
    </ErrorBoundary>
  )
}
