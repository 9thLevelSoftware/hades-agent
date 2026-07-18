import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

import { safeStderrWrite } from '../lib/safeWrite.js'

describe('safeStderrWrite', () => {
  let originalWrite: typeof process.stderr.write

  beforeEach(() => {
    originalWrite = process.stderr.write
  })

  afterEach(() => {
    process.stderr.write = originalWrite
  })

  it('writes to stderr normally when the stream is healthy', () => {
    const spy = vi.spyOn(process.stderr, 'write').mockImplementation(() => true)

    safeStderrWrite('hello\n')

    expect(spy).toHaveBeenCalledOnce()
    expect(spy).toHaveBeenCalledWith('hello\n')
  })

  it('suppresses synchronous EIO errors from a broken stream', () => {
    vi.spyOn(process.stderr, 'write').mockImplementation(() => {
      throw new Error('write EIO')
    })

    // Must not throw.
    expect(() => safeStderrWrite('lifecycle msg\n')).not.toThrow()
  })

  it('suppresses EBADF errors from a closed file descriptor', () => {
    vi.spyOn(process.stderr, 'write').mockImplementation(() => {
      throw new Error('EBADF: bad file descriptor, write')
    })

    expect(() => safeStderrWrite('signal cleanup\n')).not.toThrow()
  })

  it('suppresses generic write errors (not just EIO/EBADF)', () => {
    vi.spyOn(process.stderr, 'write').mockImplementation(() => {
      throw new Error('EPIPE: broken pipe, write')
    })

    expect(() => safeStderrWrite('any teardown output\n')).not.toThrow()
  })
})
