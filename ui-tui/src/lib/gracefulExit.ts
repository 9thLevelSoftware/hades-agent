interface SetupOptions {
  cleanups?: (() => Promise<void> | void)[]
  failsafeMs?: number
  ignoredSignals?: GracefulSignal[]
  onError?: (scope: 'uncaughtException' | 'unhandledRejection', err: unknown) => void
  onSignal?: (signal: NodeJS.Signals) => void
}

export type GracefulSignal = 'SIGHUP' | 'SIGINT' | 'SIGTERM'

const SIGNALS: readonly GracefulSignal[] = ['SIGINT', 'SIGTERM', 'SIGHUP']

const SIGNAL_EXIT_CODE: Record<GracefulSignal, number> = {
  SIGHUP: 129,
  SIGINT: 130,
  SIGTERM: 143
}

let wired = false

export const shouldExitForSignal = (signal: GracefulSignal, ignoredSignals: readonly GracefulSignal[] = []) =>
  !ignoredSignals.includes(signal)

export function setupGracefulExit({
  cleanups = [],
  failsafeMs = 4000,
  ignoredSignals = [],
  onError,
  onSignal
}: SetupOptions = {}) {
  if (wired) {
    return
  }

  wired = true

  let shuttingDown = false

  const exit = (code: number, signal?: NodeJS.Signals) => {
    if (shuttingDown) {
      return
    }

    shuttingDown = true

    if (signal) {
      try {
        onSignal?.(signal)
      } catch {
        // Signal callback (typically stderr.write) failed — the stream is
        // broken/closed.  Suppress so cleanup and exit still proceed.
      }
    }

    setTimeout(() => process.exit(code), failsafeMs).unref?.()

    void Promise.allSettled(cleanups.map(fn => Promise.resolve().then(fn))).finally(() => process.exit(code))
  }

  for (const sig of SIGNALS) {
    process.on(sig, () => {
      if (!shouldExitForSignal(sig, ignoredSignals)) {
        return
      }

      exit(SIGNAL_EXIT_CODE[sig], sig)
    })
  }

  process.on('uncaughtException', err => {
    try {
      onError?.('uncaughtException', err)
    } catch {
      // Error reporter itself failed (typically stderr.write EIO).  Suppress
      // to prevent a cascading uncaught exception from the handler.
    }
  })
  process.on('unhandledRejection', reason => {
    try {
      onError?.('unhandledRejection', reason)
    } catch {
      // Same guard as uncaughtException above.
    }
  })
}
