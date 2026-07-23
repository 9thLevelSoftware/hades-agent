// connect() must reject before WebSocket coerces garbage into
// `ws://<origin>/[object%20Object]` (#68250 stale-emit boot loop).

import { JsonRpcGatewayClient } from '@hades/shared'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class FakeSocket {
  static OPEN = 1
  readyState = 0
  addEventListener = vi.fn((type: string, handler: () => void) => {
    if (type === 'open') {
      setTimeout(() => {
        this.readyState = FakeSocket.OPEN
        handler()
      }, 0)
    }
  })
  removeEventListener = vi.fn()
  close = vi.fn()
  send = vi.fn()
}

class EarlyCloseSocket {
  readyState = 0
  private readonly handlers = new Map<string, Set<() => void>>()

  addEventListener = vi.fn((type: string, handler: () => void) => {
    let handlers = this.handlers.get(type)
    if (!handlers) {
      handlers = new Set()
      this.handlers.set(type, handlers)
    }
    handlers.add(handler)

    if (type === 'open') {
      setTimeout(() => {
        this.readyState = 3
        for (const closeHandler of [...(this.handlers.get('close') ?? [])]) {
          closeHandler()
        }
      }, 0)
    }
  })
  removeEventListener = vi.fn((type: string, handler: () => void) => {
    this.handlers.get(type)?.delete(handler)
  })
  close = vi.fn()
  send = vi.fn()
}

describe('JsonRpcGatewayClient connect() URL guard', () => {
  beforeEach(() => {
    vi.stubGlobal('WebSocket', FakeSocket) // jsdom has none; class reads WebSocket.OPEN
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('rejects a non-string IPC result object', async () => {
    const socketFactory = vi.fn()
    const client = new JsonRpcGatewayClient({ socketFactory })
    await expect(client.connect({ ok: true, wsUrl: 'ws://127.0.0.1:1/api/ws' } as unknown as string)).rejects.toThrow(
      /requires a ws:\/\/ or wss:\/\/ URL string, got type "object"/
    )
    expect(socketFactory).not.toHaveBeenCalled()
  })

  it('rejects a non-ws URL string', async () => {
    const socketFactory = vi.fn()
    const client = new JsonRpcGatewayClient({ socketFactory })
    await expect(client.connect('http://127.0.0.1:1234/api/ws')).rejects.toThrow(
      /requires a ws:\/\/ or wss:\/\/ URL string/
    )
    expect(socketFactory).not.toHaveBeenCalled()
  })

  it('rejects a malformed ws URL before opening a socket', async () => {
    const socketFactory = vi.fn()
    const client = new JsonRpcGatewayClient({ socketFactory })
    await expect(client.connect('ws://')).rejects.toThrow(/requires a ws:\/\/ or wss:\/\/ URL string/)
    expect(client.connectionState).toBe('idle')
    expect(socketFactory).not.toHaveBeenCalled()
  })

  it.each(['ws://example.com/#fragment', 'wss://example.com/api/ws#'])(
    'rejects the WebSocket-invalid fragment URL %s before opening a socket',
    async url => {
      const socketFactory = vi.fn()
      const client = new JsonRpcGatewayClient({ socketFactory })

      await expect(client.connect(url)).rejects.toThrow(/requires a ws:\/\/ or wss:\/\/ URL string/)
      expect(client.connectionState).toBe('idle')
      expect(socketFactory).not.toHaveBeenCalled()
    }
  )

  it('keeps the client retryable when socket construction throws', async () => {
    const socketFactory = vi
      .fn<(url: string) => WebSocket>()
      .mockImplementationOnce(() => {
        throw new DOMException('URL contains a fragment', 'SyntaxError')
      })
      .mockImplementation(() => new FakeSocket() as unknown as WebSocket)
    const client = new JsonRpcGatewayClient({ socketFactory })

    await expect(client.connect('ws://127.0.0.1:1234/api/ws')).rejects.toThrow(/URL contains a fragment/)
    expect(client.connectionState).toBe('idle')

    await expect(client.connect('ws://127.0.0.1:1234/api/ws')).resolves.toBeUndefined()
    expect(client.connectionState).toBe('open')
    expect(socketFactory).toHaveBeenCalledTimes(2)
  })

  it('rejects immediately when the socket closes before opening', async () => {
    vi.useFakeTimers()
    const client = new JsonRpcGatewayClient({
      connectTimeoutMs: 60_000,
      socketFactory: () => new EarlyCloseSocket() as unknown as WebSocket
    })
    let rejection: unknown
    const connection = client.connect('ws://127.0.0.1:1234/api/ws').catch(error => {
      rejection = error
    })

    await vi.advanceTimersByTimeAsync(0)

    expect(rejection).toBeInstanceOf(Error)
    expect((rejection as Error).message).toMatch(/connection failed/)
    expect(client.connectionState).toBe('closed')
    await connection
  })

  it('keeps connection state idle on rejection', async () => {
    const client = new JsonRpcGatewayClient()
    await client.connect(undefined as unknown as string).catch(() => undefined)
    expect(client.connectionState).toBe('idle')
  })

  it('accepts ws:// and wss://', async () => {
    for (const url of ['ws://127.0.0.1:1234/api/ws?token=t', 'wss://gw.example.com/api/ws?ticket=t']) {
      const client = new JsonRpcGatewayClient({ socketFactory: () => new FakeSocket() as unknown as WebSocket })
      await client.connect(url)
      expect(client.connectionState).toBe('open')
    }
  })
})
