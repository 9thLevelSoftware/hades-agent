import { beforeEach, describe, expect, it, vi } from 'vitest'

import { shouldExitForSignal } from '../lib/gracefulExit.js'

describe('shouldExitForSignal', () => {
  it('ignores only the signals explicitly disabled for embedded dashboard chat', () => {
    expect(shouldExitForSignal('SIGINT', ['SIGINT'])).toBe(false)
    expect(shouldExitForSignal('SIGTERM', ['SIGINT'])).toBe(true)
    expect(shouldExitForSignal('SIGHUP', ['SIGINT'])).toBe(true)
  })
})

describe('setupGracefulExit — callback error protection', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('onError throwing (e.g. broken stderr) does not cascade as uncaughtException', async () => {
    const { setupGracefulExit } = await import('../lib/gracefulExit.js')

    const throwingOnError = vi.fn(() => {
      throw new Error('write EIO')
    })

    setupGracefulExit({ onError: throwingOnError })

    // Emit an uncaughtException — the handler should catch the throw from
    // onError and NOT let it propagate as a second uncaught exception.
    const listeners = process.listeners('uncaughtException')
    const handler = listeners[listeners.length - 1]!

    // Call the handler directly.  If the protection is missing, this throws.
    expect(() => {
      handler(new Error('original error'))
    }).not.toThrow()

    // onError was still invoked — the original error was not swallowed.
    expect(throwingOnError).toHaveBeenCalledOnce()
  })

  it('onError throwing during unhandledRejection does not cascade', async () => {
    const { setupGracefulExit } = await import('../lib/gracefulExit.js')

    const throwingOnError = vi.fn(() => {
      throw new Error('write EIO')
    })

    setupGracefulExit({ onError: throwingOnError })

    const listeners = process.listeners('unhandledRejection')
    const handler = listeners[listeners.length - 1]!

    expect(() => {
      handler('some rejection reason')
    }).not.toThrow()

    expect(throwingOnError).toHaveBeenCalledOnce()
  })

  it('onSignal throwing (e.g. broken stderr) does not prevent cleanup scheduling', async () => {
    const { setupGracefulExit } = await import('../lib/gracefulExit.js')

    const cleanup = vi.fn()

    const throwingOnSignal = vi.fn(() => {
      throw new Error('write EIO')
    })

    setupGracefulExit({
      cleanups: [cleanup],
      onSignal: throwingOnSignal,
      failsafeMs: 50_000 // high value so it does not actually exit
    })

    const listeners = process.listeners('SIGTERM')
    const handler = listeners[listeners.length - 1]!

    // The signal handler must not throw even though onSignal does.
    expect(() => {
      handler('SIGTERM' as NodeJS.Signals)
    }).not.toThrow()
  })
})
